# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch


class _MockComponents:
    """Minimal components stub returning None for all component lookups."""

    def get_component(self, name):
        """Return None for all components (no storage in most github tests)."""
        return None


def _setup(my_predbat):
    """Reset github-specific state on my_predbat for a clean test."""
    my_predbat.github_url_cache = {}
    my_predbat.github_url_cache_loaded = True  # skip storage load in most tests
    my_predbat.components = _MockComponents()
    my_predbat.releases = {}


def _make_release(tag, name="Release", body="Notes", prerelease=False):
    """Build a minimal GitHub release dict."""
    return {"tag_name": tag, "name": name, "body": body, "prerelease": prerelease}


def test_github(my_predbat):
    """Tests for the GitHub mixin (download_predbat_releases_url, download_predbat_releases)."""
    failed = 0
    print("--- GitHub class tests ---")

    # ------------------------------------------------------------------ #
    # download_predbat_releases_url                                        #
    # ------------------------------------------------------------------ #

    print("  Test 1: cache hit returns data without making an HTTP request")
    _setup(my_predbat)
    test_url = "https://api.github.com/repos/test/releases"
    cached_data = [{"tag_name": "v1.0"}]
    my_predbat.github_url_cache[test_url] = {"stamp": datetime.now(), "data": cached_data}

    with patch("requests.get") as mock_get:
        result = my_predbat.download_predbat_releases_url(test_url)
        if mock_get.called:
            print("  FAILED: HTTP request should not be made on cache hit")
            failed += 1
    if result != cached_data:
        print("  FAILED: cache hit should return cached data, got {}".format(result))
        failed += 1

    print("  Test 2: stale cache entry triggers an HTTP request and updates cache")
    _setup(my_predbat)
    stale_stamp = datetime.now() - timedelta(hours=3)
    stale_data = [{"tag_name": "v0.9"}]
    fresh_data = [{"tag_name": "v1.1"}]
    my_predbat.github_url_cache[test_url] = {"stamp": stale_stamp, "data": stale_data}

    mock_resp = MagicMock()
    mock_resp.json.return_value = fresh_data
    with patch("requests.get", return_value=mock_resp):
        result = my_predbat.download_predbat_releases_url(test_url)
    if result != fresh_data:
        print("  FAILED: stale cache should fetch fresh data, got {}".format(result))
        failed += 1
    if my_predbat.github_url_cache.get(test_url, {}).get("data") != fresh_data:
        print("  FAILED: cache should be updated with fresh data after stale hit")
        failed += 1

    print("  Test 2b: cache entry older than 24h is treated as stale (timedelta.seconds wrap bug)")
    _setup(my_predbat)
    # 25 hours old: timedelta.seconds == 3600 (would wrongly appear fresh with age.seconds < 7200)
    old_stamp = datetime.now() - timedelta(hours=25)
    old_data = [{"tag_name": "v0.1"}]
    fresh_data2 = [{"tag_name": "v1.2"}]
    my_predbat.github_url_cache[test_url] = {"stamp": old_stamp, "data": old_data}
    mock_resp = MagicMock()
    mock_resp.json.return_value = fresh_data2
    with patch("requests.get", return_value=mock_resp):
        result = my_predbat.download_predbat_releases_url(test_url)
    if result != fresh_data2:
        print("  FAILED: 25h-old cache entry should be treated as stale, got {}".format(result))
        failed += 1

    print("  Test 2c: corrupted cache entry (missing stamp/data) falls through to HTTP fetch")
    _setup(my_predbat)
    my_predbat.github_url_cache[test_url] = {"unexpected_key": "garbage"}
    fetched = [{"tag_name": "v1.3"}]
    mock_resp = MagicMock()
    mock_resp.json.return_value = fetched
    with patch("requests.get", return_value=mock_resp):
        result = my_predbat.download_predbat_releases_url(test_url)
    if result != fetched:
        print("  FAILED: corrupted cache entry should fall through to HTTP fetch, got {}".format(result))
        failed += 1

    print("  Test 3: HTTP exception returns empty list and does not populate cache")
    _setup(my_predbat)
    with patch("requests.get", side_effect=Exception("network error")):
        result = my_predbat.download_predbat_releases_url(test_url)
    if result != []:
        print("  FAILED: HTTP exception should return [], got {}".format(result))
        failed += 1
    if test_url in my_predbat.github_url_cache:
        print("  FAILED: cache should not be populated after HTTP error")
        failed += 1

    print("  Test 4: JSON decode error returns empty list and does not populate cache")
    _setup(my_predbat)
    mock_resp = MagicMock()
    import requests as _requests
    mock_resp.json.side_effect = _requests.exceptions.JSONDecodeError("bad json", "", 0)
    with patch("requests.get", return_value=mock_resp):
        result = my_predbat.download_predbat_releases_url(test_url)
    if result != []:
        print("  FAILED: JSON error should return [], got {}".format(result))
        failed += 1
    if test_url in my_predbat.github_url_cache:
        print("  FAILED: cache should not be populated after JSON error")
        failed += 1

    print("  Test 5: cache miss fetches from GitHub and stores result")
    _setup(my_predbat)
    fetched_data = [{"tag_name": "v2.0"}]
    mock_resp = MagicMock()
    mock_resp.json.return_value = fetched_data
    with patch("requests.get", return_value=mock_resp):
        result = my_predbat.download_predbat_releases_url(test_url)
    if result != fetched_data:
        print("  FAILED: cache miss should return fetched data, got {}".format(result))
        failed += 1
    if my_predbat.github_url_cache.get(test_url, {}).get("data") != fetched_data:
        print("  FAILED: fetched data should be stored in cache")
        failed += 1

    # ------------------------------------------------------------------ #
    # download_predbat_releases                                            #
    # ------------------------------------------------------------------ #

    print("  Test 6: download_predbat_releases parses latest stable and beta versions")
    _setup(my_predbat)
    releases_data = [
        _make_release("v2.0", "Stable Two", prerelease=False),
        _make_release("v1.9-beta", "Beta", prerelease=True),
        _make_release("v1.8", "Stable One", prerelease=False),
    ]
    with patch.object(my_predbat, "download_predbat_releases_url", return_value=releases_data):
        with patch.object(my_predbat, "expose_config"):
            with patch.object(my_predbat, "download_predbat_version"):
                my_predbat.download_predbat_releases()

    if my_predbat.releases.get("latest") != "v2.0":
        print("  FAILED: latest should be v2.0, got {}".format(my_predbat.releases.get("latest")))
        failed += 1
    if my_predbat.releases.get("latest_beta") != "v2.0":
        print("  FAILED: latest_beta should be v2.0 (first entry), got {}".format(my_predbat.releases.get("latest_beta")))
        failed += 1

    print("  Test 7: download_predbat_releases sets latest_beta to prerelease when it comes first")
    _setup(my_predbat)
    releases_data = [
        _make_release("v2.1-beta", "Beta", prerelease=True),
        _make_release("v2.0", "Stable", prerelease=False),
    ]
    with patch.object(my_predbat, "download_predbat_releases_url", return_value=releases_data):
        with patch.object(my_predbat, "expose_config"):
            with patch.object(my_predbat, "download_predbat_version"):
                my_predbat.download_predbat_releases()

    if my_predbat.releases.get("latest") != "v2.0":
        print("  FAILED: latest should be v2.0, got {}".format(my_predbat.releases.get("latest")))
        failed += 1
    if my_predbat.releases.get("latest_beta") != "v2.1-beta":
        print("  FAILED: latest_beta should be v2.1-beta, got {}".format(my_predbat.releases.get("latest_beta")))
        failed += 1

    print("  Test 8: download_predbat_releases handles empty/non-list data gracefully")
    for bad_data in [[], None, "error", {}]:
        _setup(my_predbat)
        expose_calls = []
        with patch.object(my_predbat, "download_predbat_releases_url", return_value=bad_data):
            with patch.object(my_predbat, "expose_config", side_effect=lambda k, v, **kw: expose_calls.append((k, v))):
                my_predbat.download_predbat_releases()
        version_calls = [v for k, v in expose_calls if k == "version"]
        if not version_calls or version_calls[-1] is not False:
            print("  FAILED: version should be set to False for bad data {}, expose calls: {}".format(bad_data, expose_calls))
            failed += 1

    print("  Test 9: auto_update triggers download_predbat_version when a newer release is available")
    _setup(my_predbat)
    from predbat import THIS_VERSION as CURRENT_VERSION
    releases_data = [
        _make_release("v99.0.0", "Future", prerelease=False),
        _make_release(CURRENT_VERSION, "This", prerelease=False),
    ]
    if "auto_update" in my_predbat.config_index:
        my_predbat.config_index["auto_update"]["value"] = True
    download_calls = []
    with patch.object(my_predbat, "download_predbat_releases_url", return_value=releases_data):
        with patch.object(my_predbat, "expose_config"):
            with patch.object(my_predbat, "download_predbat_version", side_effect=lambda v: download_calls.append(v)):
                my_predbat.download_predbat_releases()
    if not download_calls:
        print("  FAILED: download_predbat_version should be called when auto_update is on and newer version exists")
        failed += 1

    print("  Test 10: auto_update off does not trigger download even when newer version available")
    _setup(my_predbat)
    if "auto_update" in my_predbat.config_index:
        my_predbat.config_index["auto_update"]["value"] = False
    download_calls = []
    with patch.object(my_predbat, "download_predbat_releases_url", return_value=releases_data):
        with patch.object(my_predbat, "expose_config"):
            with patch.object(my_predbat, "download_predbat_version", side_effect=lambda v: download_calls.append(v)):
                my_predbat.download_predbat_releases()
    if download_calls:
        print("  FAILED: download_predbat_version should NOT be called when auto_update is off")
        failed += 1

    print("  Test 11: _save_github_url_cache_to_storage prunes entries with non-datetime stamp without raising")
    import shutil
    import tempfile
    from storage import StorageComponent, StorageLocalFiles

    class _MockComponentsWithStorage:
        """Minimal components mock returning a real storage instance."""

        def __init__(self, storage):
            """Initialise with storage."""
            self._storage = storage

        def get_component(self, name):
            """Return storage for 'storage' key."""
            return self._storage if name == "storage" else None

    tmpdir = tempfile.mkdtemp()
    try:
        storage = StorageComponent(my_predbat)
        storage.backend = StorageLocalFiles(tmpdir, my_predbat.log)
        my_predbat.components = _MockComponentsWithStorage(storage)
        my_predbat.github_url_cache = {
            "https://good.example/": {"stamp": datetime.now(), "data": [1]},
            "https://string-stamp.example/": {"stamp": "2024-01-01T00:00:00", "data": [2]},
            "https://missing-stamp.example/": {"data": [3]},
            "https://not-a-dict.example/": "corrupted",
        }
        try:
            my_predbat._save_github_url_cache_to_storage()
        except Exception as exc:
            print("  FAILED: _save_github_url_cache_to_storage raised unexpectedly: {}".format(exc))
            failed += 1
        else:
            remaining = list(my_predbat.github_url_cache.keys())
            if "https://good.example/" not in remaining:
                print("  FAILED: valid entry should survive pruning, remaining: {}".format(remaining))
                failed += 1
            if "https://string-stamp.example/" in remaining:
                print("  FAILED: string-stamp entry should be pruned, remaining: {}".format(remaining))
                failed += 1
            if "https://missing-stamp.example/" in remaining:
                print("  FAILED: missing-stamp entry should be pruned, remaining: {}".format(remaining))
                failed += 1
            if "https://not-a-dict.example/" in remaining:
                print("  FAILED: non-dict entry should be pruned, remaining: {}".format(remaining))
                failed += 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("  Test 12: _load_github_url_cache_from_storage ignores non-dict data without crashing")
    import shutil
    import tempfile
    from storage import StorageComponent, StorageLocalFiles
    from tests.test_infra import run_async as _run_async

    tmpdir2 = tempfile.mkdtemp()
    try:
        storage2 = StorageComponent(my_predbat)
        storage2.backend = StorageLocalFiles(tmpdir2, my_predbat.log)
        my_predbat.components = _MockComponentsWithStorage(storage2)

        # Write a non-dict value directly into storage
        _run_async(storage2.save("predbat", "github_url_cache", ["not", "a", "dict"], format="json"))

        my_predbat.github_url_cache = {}
        my_predbat.github_url_cache_loaded = False
        try:
            my_predbat._load_github_url_cache_from_storage()
        except Exception as exc:
            print("  FAILED: _load raised unexpectedly on bad stored type: {}".format(exc))
            failed += 1
        else:
            if my_predbat.github_url_cache:
                print("  FAILED: non-dict storage value should not be loaded into cache")
                failed += 1
    finally:
        shutil.rmtree(tmpdir2, ignore_errors=True)

    return failed
