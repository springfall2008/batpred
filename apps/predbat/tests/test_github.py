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
from download import MAX_PREDBAT_BRANCH_OPTIONS


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

    print("  Test 4b: non-2xx status (e.g. rate limited) with no prior cache returns [] and does not cache the failure")
    _setup(my_predbat)
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 403
    with patch("requests.get", return_value=mock_resp):
        result = my_predbat.download_predbat_releases_url(test_url)
    if result != []:
        print("  FAILED: non-2xx status with no prior cache should return [], got {}".format(result))
        failed += 1
    if test_url in my_predbat.github_url_cache:
        print("  FAILED: cache should not be populated after a non-2xx response")
        failed += 1

    print("  Test 4c: non-2xx status falls back to stale cached data instead of blanking it out for the full cache window")
    _setup(my_predbat)
    stale_stamp2 = datetime.now() - timedelta(hours=3)
    stale_data2 = [{"tag_name": "v0.9"}]
    my_predbat.github_url_cache[test_url] = {"stamp": stale_stamp2, "data": stale_data2}
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 403
    with patch("requests.get", return_value=mock_resp):
        result = my_predbat.download_predbat_releases_url(test_url)
    if result != stale_data2:
        print("  FAILED: non-2xx status should fall back to stale cached data, got {}".format(result))
        failed += 1
    # The failed fetch must not overwrite the stamp, so the next cycle retries rather than waiting out the full cache window
    if my_predbat.github_url_cache.get(test_url, {}).get("stamp") != stale_stamp2:
        print("  FAILED: a failed fetch should not refresh the cache stamp")
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

    print("  Test 13: predbat_branch adds 'branch: <name>' entries after main in the update options")
    releases_data = [_make_release("v2.0", "Stable Two", prerelease=False)]

    def _options_for(branch):
        _setup(my_predbat)
        if branch is None:
            my_predbat.args.pop("predbat_branch", None)
        else:
            my_predbat.args["predbat_branch"] = branch
        with patch.object(my_predbat, "download_predbat_releases_url", return_value=releases_data):
            with patch.object(my_predbat, "expose_config"):
                with patch.object(my_predbat, "download_predbat_version"):
                    my_predbat.download_predbat_releases()
        return list(my_predbat.config_index["update"]["options"])

    try:
        options = _options_for("my-feature")
        if options[:2] != ["main", "branch: my-feature"] or not any(o.startswith("v2.0") for o in options):
            print("  FAILED: expected main, then branch: my-feature, then the releases, got {}".format(options))
            failed += 1

        print("  Test 14: a list of branches, including names with a /, is offered in the order given without duplicates or blanks")
        options = _options_for(["fix/one", "feat/two", "fix/one", "  ", "test/three"])
        if options[:4] != ["main", "branch: fix/one", "branch: feat/two", "branch: test/three"] or not any(o.startswith("v2.0") for o in options):
            print("  FAILED: expected main, three branch entries and the releases, got {}".format(options))
            failed += 1

        print("  Test 15: no branch entry when predbat_branch is not set, is main, or is not a valid branch name")
        for branch in [None, "", "   ", [], "main", ["main"], "has space", "a//b", "/x", 5]:
            options = _options_for(branch)
            if any(o.startswith("branch:") for o in options):
                print("  FAILED: predbat_branch {!r} should not add a branch entry, got {}".format(branch, options))
                failed += 1
            if options[0] != "main":
                print("  FAILED: main should always be the first option, got {}".format(options))
                failed += 1

        print("  Test 16: an invalid branch in the list is skipped but the valid ones are kept")
        options = _options_for(["fix/ok", "has space", "feat/ok"])
        if [o for o in options if o.startswith("branch:")] != ["branch: fix/ok", "branch: feat/ok"]:
            print("  FAILED: expected only the two valid branches, got {}".format(options))
            failed += 1

        print("  Test 17: no more than MAX_PREDBAT_BRANCH_OPTIONS branches are offered, and releases are still listed")
        many = ["fix/b{}".format(n) for n in range(MAX_PREDBAT_BRANCH_OPTIONS + 5)]
        options = _options_for(many)
        offered = [o for o in options if o.startswith("branch:")]
        if offered != ["branch: " + b for b in many[:MAX_PREDBAT_BRANCH_OPTIONS]]:
            print("  FAILED: expected the first {} branches only, got {}".format(MAX_PREDBAT_BRANCH_OPTIONS, offered))
            failed += 1
        if not any(o.startswith("v2.0") for o in options):
            print("  FAILED: releases must still be listed after the branches, got {}".format(options))
            failed += 1

        print("  Test 18: predbat_branch warnings are logged once per run, not every plan cycle")
        my_predbat.predbat_branch_warned = set()
        with patch.object(my_predbat, "log") as mock_log:
            for _cycle in range(3):
                _options_for(["has space", "fix/ok"])
        warnings = [call for call in mock_log.call_args_list if "predbat_branch" in str(call)]
        if len(warnings) != 1:
            print("  FAILED: expected the invalid branch warning once over 3 cycles, got {}: {}".format(len(warnings), warnings))
            failed += 1

        my_predbat.predbat_branch_warned = set()
        with patch.object(my_predbat, "log") as mock_log:
            for _cycle in range(3):
                _options_for(many)
        warnings = [call for call in mock_log.call_args_list if "predbat_branch" in str(call)]
        if len(warnings) != 5:
            print("  FAILED: expected one over-limit warning for each of the 5 branches that are left out, got {}".format(len(warnings)))
            failed += 1

        my_predbat.predbat_branch_warned = set()
        with patch.object(my_predbat, "log") as mock_log:
            _options_for(["has space"])
            _options_for(["has another space"])
        warnings = [call for call in mock_log.call_args_list if "predbat_branch" in str(call)]
        if len(warnings) != 2:
            print("  FAILED: two different invalid branches should each be warned about once, got {}".format(len(warnings)))
            failed += 1
    finally:
        my_predbat.args.pop("predbat_branch", None)
        my_predbat.predbat_branch_warned = set()

    return failed


def test_update_options_are_per_engine(my_predbat):
    """Several engine instances in one process each build their own update dropdown options."""
    import config as _config
    import github as _github
    from github import GitHub

    failed = 0
    print("--- update options are per engine ---")

    sentinel = object()
    config_before = list(_config.PREDBAT_UPDATE_OPTIONS)
    module_before = getattr(_github, "PREDBAT_UPDATE_OPTIONS", sentinel)
    module_before_copy = list(module_before) if isinstance(module_before, list) else module_before

    class _FakeEngine:
        """Borrows download_predbat_releases from the real class, with the network and config stubbed."""

        download_predbat_releases = GitHub.download_predbat_releases

        def __init__(self, releases):
            self.releases_data = releases
            self.args = {}
            self.config_index = {"update": {"options": ["Loading..."], "value": "x"}}
            self.logs = []

        def get_arg(self, name, default=None):
            return default

        def download_predbat_releases_url(self, url):
            return self.releases_data

        def get_predbat_branches(self):
            return []

        def log(self, message):
            self.logs.append(message)

        def download_predbat_version(self, version):
            pass

        def expose_config(self, name, value, **kwargs):
            pass

    engine_a = _FakeEngine([_make_release("va1.0", "Alpha One"), _make_release("va0.9", "Alpha Zero")])
    engine_b = _FakeEngine([_make_release("vb2.0", "Beta Two")])

    engine_a.download_predbat_releases()
    engine_b.download_predbat_releases()

    options_a = engine_a.config_index["update"]["options"]
    options_b = engine_b.config_index["update"]["options"]

    if options_a is options_b:
        print("  FAILED: the two engines share one options list object")
        failed += 1
    if not any(o.startswith("va1.0") for o in options_a) or any(o.startswith("vb2.0") for o in options_a):
        print("  FAILED: engine A options should hold only its own releases, got {}".format(options_a))
        failed += 1
    if not any(o.startswith("vb2.0") for o in options_b) or any(o.startswith("va") for o in options_b):
        print("  FAILED: engine B options should hold only its own releases, got {}".format(options_b))
        failed += 1
    # Neither engine runs the current version, so the "(?)" entry must land in the list handed to the dropdown
    if not options_a[-1].endswith("(?)") or not options_b[-1].endswith("(?)"):
        print("  FAILED: the unknown version entry should be appended to each engine's own options, got {} and {}".format(options_a, options_b))
        failed += 1
    module_after = getattr(_github, "PREDBAT_UPDATE_OPTIONS", sentinel)
    if module_after is not module_before or (isinstance(module_after, list) and module_after != module_before_copy):
        print("  FAILED: github.PREDBAT_UPDATE_OPTIONS module attribute was changed by the calls")
        failed += 1
    if _config.PREDBAT_UPDATE_OPTIONS != config_before:
        print("  FAILED: config.PREDBAT_UPDATE_OPTIONS was mutated, got {}".format(_config.PREDBAT_UPDATE_OPTIONS))
        failed += 1

    if failed:
        print("  FAILED: test_update_options_are_per_engine")
    else:
        print("  OK: test_update_options_are_per_engine")
    return failed
