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
from datetime import datetime, timedelta, timezone

from utils import pack_export_limit
from const import EXPORT_MODE_TARGET
from tests.test_infra import run_async, MockStorageComponents, make_test_storage


def _execution_state_env(my_predbat):
    tmpdir = tempfile.mkdtemp()
    storage = make_test_storage(my_predbat, tmpdir)
    my_predbat.components = MockStorageComponents(storage)
    my_predbat.execution_state_saved = None
    return tmpdir, storage


def _set_flags(my_predbat, charging, charge_target, exporting, export_target):
    my_predbat.isCharging = charging
    my_predbat.isCharging_Target = charge_target
    my_predbat.isExporting = exporting
    my_predbat.isExporting_Target = export_target


def test_execution_state_roundtrip(my_predbat):
    tmpdir, _ = _execution_state_env(my_predbat)
    try:
        _set_flags(my_predbat, True, 80, True, 25)
        assert my_predbat.save_execution_state() is True
        _set_flags(my_predbat, False, 0, False, 0)
        my_predbat.execution_state_saved = None
        my_predbat.load_execution_state()
        assert (my_predbat.isCharging, my_predbat.isCharging_Target, my_predbat.isExporting, my_predbat.isExporting_Target) == (True, 80, True, 25)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_execution_state_absent(my_predbat):
    tmpdir, storage = _execution_state_env(my_predbat)
    try:
        _set_flags(my_predbat, False, 0, False, 0)
        my_predbat.load_execution_state()
        assert (my_predbat.isCharging, my_predbat.isCharging_Target, my_predbat.isExporting, my_predbat.isExporting_Target) == (False, 0, False, 0)
        # A malformed item is ignored too
        run_async(storage.save("predbat", "execution_state", ["nonsense"], format="json"))
        my_predbat.load_execution_state()
        assert (my_predbat.isCharging, my_predbat.isExporting) == (False, False)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_execution_state_does_not_bump_plan_version(my_predbat):
    tmpdir, storage = _execution_state_env(my_predbat)
    try:
        my_predbat.now_utc = datetime.now(timezone.utc)
        my_predbat.charge_window_best = [{"start": 480, "end": 600, "average": 14.5}]
        my_predbat.charge_limit_best = [8.5]
        my_predbat.export_window_best = []
        my_predbat.export_limits_best = []
        my_predbat.plan_preclip = None
        my_predbat.plan_last_updated = my_predbat.now_utc
        my_predbat.plan_valid = True
        my_predbat.plan_version = 0
        my_predbat.plan_version_content = None
        assert my_predbat.save_plan_if_changed() is True
        version = my_predbat.plan_version
        content = my_predbat.plan_version_content
        saved_plan = run_async(storage.load("predbat", "plan"))
        _set_flags(my_predbat, False, 0, True, 40)
        assert my_predbat.save_execution_state() is True
        assert my_predbat.plan_version == version
        assert my_predbat.plan_version_content == content
        assert run_async(storage.load("predbat", "plan")) == saved_plan
        assert my_predbat.save_plan_if_changed() is False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_execution_state_saves_only_on_change(my_predbat):
    tmpdir, _ = _execution_state_env(my_predbat)
    try:
        _set_flags(my_predbat, True, 100, False, 0)
        assert my_predbat.save_execution_state() is True
        assert my_predbat.save_execution_state() is False
        my_predbat.isCharging = False
        assert my_predbat.save_execution_state() is True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


EXECUTION_STATE_TESTS = [
    test_execution_state_roundtrip,
    test_execution_state_absent,
    test_execution_state_does_not_bump_plan_version,
    test_execution_state_saves_only_on_change,
]


def test_plan_persistence(my_predbat):
    """Test save_plan() and load_plan() round-trip and edge cases."""
    failed = 0
    print("--- Plan persistence tests ---")

    tmpdir = tempfile.mkdtemp()
    try:
        storage = make_test_storage(my_predbat, tmpdir)
        my_predbat.components = MockStorageComponents(storage)

        # Drive the fixture clock a long way from the real one for the round trip below. Both
        # clocks agreeing is what let the wrong-clock bug hide: an expiry of now_utc + 8h is
        # only wrong once the two have drifted more than 8 hours apart, so with the fixture
        # pinned near real time the test passed either way for most of the day. Pinned a year
        # back, save_plan() computing expiry from now_utc always writes an already-expired
        # plan and Test 1 fails at any hour.
        my_predbat.now_utc = datetime.now(timezone.utc) - timedelta(days=365)

        # --- build a representative plan ---
        charge_windows = [{"start": 480, "end": 600, "average": 14.5}]
        charge_limits = [8.5]
        export_windows = [{"start": 720, "end": 840, "average": 45.0}]
        export_limits = [pack_export_limit(EXPORT_MODE_TARGET, 50)]
        saved_minutes = my_predbat.minutes_now

        my_predbat.charge_window_best = charge_windows
        my_predbat.charge_limit_best = charge_limits
        my_predbat.export_window_best = export_windows
        my_predbat.export_limits_best = export_limits
        my_predbat.plan_last_updated = my_predbat.now_utc
        my_predbat.plan_last_updated_minutes = saved_minutes
        my_predbat.plan_valid = True

        # The pre-clip snapshot is what plan selection scores against next cycle, so it has to survive a
        # restart too - without it the first recompute back compares a clipped incumbent and falls back
        preclip = ([9.0], charge_windows, export_windows, [pack_export_limit(EXPORT_MODE_TARGET, 0)])
        my_predbat.plan_preclip = preclip
        # The version that made the plan has to survive a restart, so the first recompute back knows whether
        # the restored plan came from this code or an older one
        my_predbat.plan_made_by_version = "v9.9.9 (test build)"

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
        my_predbat.plan_preclip = None
        my_predbat.plan_made_by_version = None

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
        if my_predbat.plan_preclip is None:
            print("  FAILED: plan_preclip was not restored")
            failed += 1
        elif [list(x) for x in my_predbat.plan_preclip] != [list(x) for x in preclip]:
            print("  FAILED: plan_preclip mismatch: {}".format(my_predbat.plan_preclip))
            failed += 1
        if my_predbat.plan_made_by_version != "v9.9.9 (test build)":
            print("  FAILED: plan_made_by_version was not restored: {}".format(my_predbat.plan_made_by_version))
            failed += 1

        # 1b. A plan saved before the version was recorded is still restored, made by an unknown version
        print("  Test 1b: load_plan() restores a plan saved without a version")
        tmpdir1b = tempfile.mkdtemp()
        try:
            storage1b = make_test_storage(my_predbat, tmpdir1b)
            my_predbat.components = MockStorageComponents(storage1b)
            plan_data = {
                "charge_window_best": [{"start": 120, "end": 240, "average": 10.0}],
                "charge_limit_best": [5.0],
                "export_window_best": [],
                "export_limits_best": [],
                "plan_last_updated": my_predbat.now_utc.isoformat(),
                "plan_last_updated_minutes": 60,
            }
            run_async(storage1b.save("predbat", "plan", plan_data, format="json", expiry=datetime.now(timezone.utc) + timedelta(hours=8)))
            my_predbat.plan_valid = False
            my_predbat.plan_made_by_version = "stale"
            my_predbat.load_plan()
            if not my_predbat.plan_valid or my_predbat.charge_limit_best != [5.0]:
                print("  FAILED: a plan saved without a version should still be restored")
                failed += 1
            if my_predbat.plan_made_by_version is not None:
                print("  FAILED: plan_made_by_version should be None for a plan saved without a version, got {}".format(my_predbat.plan_made_by_version))
                failed += 1
        finally:
            shutil.rmtree(tmpdir1b, ignore_errors=True)
            my_predbat.components = MockStorageComponents(storage)

        # 2. load_plan() with empty storage leaves plan_valid False
        print("  Test 2: load_plan() with no saved plan")
        tmpdir2 = tempfile.mkdtemp()
        try:
            my_predbat.components = MockStorageComponents(make_test_storage(my_predbat, tmpdir2))
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
            storage2b = make_test_storage(my_predbat, tmpdir2b)
            my_predbat.components = MockStorageComponents(storage2b)
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
        my_predbat.components = MockStorageComponents(None)
        my_predbat.plan_valid = False
        my_predbat.load_plan()
        if my_predbat.plan_valid:
            print("  FAILED: plan_valid should remain False when storage is unavailable")
            failed += 1

        # 4. save_plan() with storage component unavailable is a safe no-op
        print("  Test 4: save_plan() with storage unavailable")
        my_predbat.components = MockStorageComponents(None)
        try:
            my_predbat.save_plan()
        except Exception as e:
            print("  FAILED: save_plan() raised unexpected exception: {}".format(e))
            failed += 1

        # 5. load_plan() treats an expired storage entry as missing
        print("  Test 5: load_plan() with expired stored plan")
        tmpdir3 = tempfile.mkdtemp()
        try:
            storage3 = make_test_storage(my_predbat, tmpdir3)
            my_predbat.components = MockStorageComponents(storage3)

            # Opposite pin to Test 1: a fixture clock in the future, so building the "already
            # expired" timestamp from now_utc would put it ahead of real time and the plan
            # would load. That makes the wrong-clock version of the line below fail at any
            # hour rather than only before ~11:00 local.
            my_predbat.now_utc = datetime.now(timezone.utc) + timedelta(days=365)

            plan_data = {
                "charge_window_best": [{"start": 120, "end": 240, "average": 10.0}],
                "charge_limit_best": [5.0],
                "export_window_best": [],
                "export_limits_best": [],
                "plan_last_updated": my_predbat.now_utc.isoformat(),
                "plan_last_updated_minutes": 60,
            }
            # Expiry has to be in the past relative to the clock storage.load() actually checks
            # against, which is the real one rather than Predbat's (#5079).
            past_expiry = datetime.now(timezone.utc) - timedelta(hours=1)
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
            storage4 = make_test_storage(my_predbat, tmpdir4)
            my_predbat.components = MockStorageComponents(storage4)
            my_predbat.github_url_cache_loaded = False
            my_predbat.github_url_cache = {}

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

    saved_flags = (my_predbat.isCharging, my_predbat.isCharging_Target, my_predbat.isExporting, my_predbat.isExporting_Target)
    for execution_test in EXECUTION_STATE_TESTS:
        try:
            execution_test(my_predbat)
            print("OK: {}".format(execution_test.__name__))
        except Exception as e:
            print("FAILED: {}: {!r}".format(execution_test.__name__, e))
            failed += 1
    my_predbat.isCharging, my_predbat.isCharging_Target, my_predbat.isExporting, my_predbat.isExporting_Target = saved_flags
    my_predbat.execution_state_saved = None

    return failed
