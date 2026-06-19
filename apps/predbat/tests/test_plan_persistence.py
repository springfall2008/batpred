# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import shutil
import tempfile
from datetime import timedelta

from storage import StorageComponent, StorageLocalFiles
from tests.test_infra import run_async


class _MockComponents:
    """Minimal components mock returning a pre-configured storage component."""

    def __init__(self, storage):
        """Initialise with a storage instance (may be None to simulate unavailable)."""
        self._storage = storage

    def get_component(self, name):
        """Return the mocked storage for 'storage', None for everything else."""
        if name == "storage":
            return self._storage
        return None


def _make_storage(predbat, tmpdir):
    """Create a StorageComponent backed by a real local-file backend in tmpdir."""
    storage = StorageComponent(predbat)
    storage.backend = StorageLocalFiles(tmpdir, predbat.log)
    return storage


def test_plan_persistence(my_predbat):
    """Test save_plan() and load_plan() round-trip and edge cases."""
    failed = 0
    print("--- Plan persistence tests ---")

    tmpdir = tempfile.mkdtemp()
    try:
        storage = _make_storage(my_predbat, tmpdir)
        my_predbat.components = _MockComponents(storage)

        # --- build a representative plan ---
        charge_windows = [{"start": 480, "end": 600, "average": 14.5}]
        charge_limits = [8.5]
        export_windows = [{"start": 720, "end": 840, "average": 45.0}]
        export_limits = [50.0]
        saved_minutes = my_predbat.minutes_now

        my_predbat.charge_window_best = charge_windows
        my_predbat.charge_limit_best = charge_limits
        my_predbat.export_window_best = export_windows
        my_predbat.export_limits_best = export_limits
        my_predbat.plan_last_updated = my_predbat.now_utc
        my_predbat.plan_last_updated_minutes = saved_minutes
        my_predbat.plan_valid = True

        # 1. save_plan() must not raise and must write something loadable
        print("  Test 1: save_plan() round-trip")
        my_predbat.save_plan()

        # Clear state so load_plan() has something to restore
        my_predbat.charge_window_best = []
        my_predbat.charge_limit_best = []
        my_predbat.export_window_best = []
        my_predbat.export_limits_best = []
        my_predbat.plan_last_updated = None
        my_predbat.plan_last_updated_minutes = 0
        my_predbat.plan_valid = False

        my_predbat.load_plan()

        if not my_predbat.plan_valid:
            print("  FAILED: plan_valid should be True after load_plan()")
            failed += 1
        if my_predbat.charge_window_best != charge_windows:
            print("  FAILED: charge_window_best mismatch: {}".format(my_predbat.charge_window_best))
            failed += 1
        if my_predbat.charge_limit_best != charge_limits:
            print("  FAILED: charge_limit_best mismatch: {}".format(my_predbat.charge_limit_best))
            failed += 1
        if my_predbat.export_window_best != export_windows:
            print("  FAILED: export_window_best mismatch: {}".format(my_predbat.export_window_best))
            failed += 1
        if my_predbat.export_limits_best != export_limits:
            print("  FAILED: export_limits_best mismatch: {}".format(my_predbat.export_limits_best))
            failed += 1
        if my_predbat.plan_last_updated_minutes != saved_minutes:
            print("  FAILED: plan_last_updated_minutes mismatch: {}".format(my_predbat.plan_last_updated_minutes))
            failed += 1

        # 2. load_plan() with empty storage leaves plan_valid False
        print("  Test 2: load_plan() with no saved plan")
        tmpdir2 = tempfile.mkdtemp()
        try:
            my_predbat.components = _MockComponents(_make_storage(my_predbat, tmpdir2))
            my_predbat.plan_valid = False
            my_predbat.load_plan()
            if my_predbat.plan_valid:
                print("  FAILED: plan_valid should remain False when storage has no plan")
                failed += 1
        finally:
            shutil.rmtree(tmpdir2, ignore_errors=True)

        # 2b. load_plan() with a non-dict stored value is a safe no-op
        print("  Test 2b: load_plan() ignores non-dict stored plan")
        tmpdir2b = tempfile.mkdtemp()
        try:
            storage2b = _make_storage(my_predbat, tmpdir2b)
            my_predbat.components = _MockComponents(storage2b)
            run_async(storage2b.save("predbat", "plan", ["not", "a", "dict"], format="json"))
            my_predbat.plan_valid = False
            try:
                my_predbat.load_plan()
            except Exception as exc:
                print("  FAILED: load_plan() raised unexpectedly on non-dict stored data: {}".format(exc))
                failed += 1
            if my_predbat.plan_valid:
                print("  FAILED: plan_valid should remain False for non-dict stored plan")
                failed += 1
        finally:
            shutil.rmtree(tmpdir2b, ignore_errors=True)

        # 3. load_plan() with storage component unavailable is a safe no-op
        print("  Test 3: load_plan() with storage unavailable")
        my_predbat.components = _MockComponents(None)
        my_predbat.plan_valid = False
        my_predbat.load_plan()
        if my_predbat.plan_valid:
            print("  FAILED: plan_valid should remain False when storage is unavailable")
            failed += 1

        # 4. save_plan() with storage component unavailable is a safe no-op
        print("  Test 4: save_plan() with storage unavailable")
        my_predbat.components = _MockComponents(None)
        try:
            my_predbat.save_plan()
        except Exception as e:
            print("  FAILED: save_plan() raised unexpected exception: {}".format(e))
            failed += 1

        # 5. load_plan() treats an expired storage entry as missing
        print("  Test 5: load_plan() with expired stored plan")
        tmpdir3 = tempfile.mkdtemp()
        try:
            storage3 = _make_storage(my_predbat, tmpdir3)
            my_predbat.components = _MockComponents(storage3)

            plan_data = {
                "charge_window_best": [{"start": 120, "end": 240, "average": 10.0}],
                "charge_limit_best": [5.0],
                "export_window_best": [],
                "export_limits_best": [],
                "plan_last_updated": my_predbat.now_utc.isoformat(),
                "plan_last_updated_minutes": 60,
            }
            past_expiry = my_predbat.now_utc - timedelta(hours=1)
            run_async(storage3.save("predbat", "plan", plan_data, format="json", expiry=past_expiry))

            my_predbat.plan_valid = False
            my_predbat.charge_window_best = []
            my_predbat.load_plan()

            if my_predbat.plan_valid:
                print("  FAILED: plan_valid should remain False for expired stored plan")
                failed += 1
            if my_predbat.charge_window_best:
                print("  FAILED: charge_window_best should remain empty for expired stored plan")
                failed += 1
        finally:
            shutil.rmtree(tmpdir3, ignore_errors=True)

        # 6. GitHub URL cache round-trip
        print("  Test 6: GitHub URL cache save/load round-trip")
        tmpdir4 = tempfile.mkdtemp()
        try:
            storage4 = _make_storage(my_predbat, tmpdir4)
            my_predbat.components = _MockComponents(storage4)
            my_predbat.github_url_cache_loaded = False
            my_predbat.github_url_cache = {}

            from datetime import datetime
            test_url = "https://api.github.com/repos/test/releases"
            my_predbat.github_url_cache[test_url] = {"stamp": datetime.now(), "data": [{"tag_name": "v1.0"}]}
            my_predbat._save_github_url_cache_to_storage()

            # Clear and reload
            my_predbat.github_url_cache = {}
            my_predbat.github_url_cache_loaded = False
            my_predbat._load_github_url_cache_from_storage()

            if test_url not in my_predbat.github_url_cache:
                print("  FAILED: GitHub URL cache entry missing after load")
                failed += 1
            elif my_predbat.github_url_cache[test_url].get("data") != [{"tag_name": "v1.0"}]:
                print("  FAILED: GitHub URL cache data mismatch after load")
                failed += 1

            # 7. Second call to _load_github_url_cache_from_storage() is a no-op (cache_loaded flag)
            print("  Test 7: GitHub URL cache load is skipped when already loaded")
            my_predbat.github_url_cache = {}
            my_predbat._load_github_url_cache_from_storage()
            if my_predbat.github_url_cache:
                print("  FAILED: second load should be skipped via github_url_cache_loaded flag")
                failed += 1

            # 8. Stale entries are pruned on save
            print("  Test 8: stale GitHub URL cache entries are pruned on save")
            old_url = "https://api.github.com/repos/old/releases"
            my_predbat.github_url_cache[old_url] = {"stamp": datetime.now() - timedelta(hours=25), "data": []}
            my_predbat._save_github_url_cache_to_storage()
            if old_url in my_predbat.github_url_cache:
                print("  FAILED: stale GitHub cache entry should be pruned")
                failed += 1

        finally:
            shutil.rmtree(tmpdir4, ignore_errors=True)

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return failed
