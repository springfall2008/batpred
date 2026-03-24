# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import os
import json
import shutil
from datetime import datetime, timedelta
from rate_store import RateStore


def run_rate_store_tests(my_predbat):
    """
    Run comprehensive tests for rate persistence with freeze-past-slots logic

    Args:
        my_predbat: PredBat instance (unused for these tests but required for consistency)

    Returns:
        bool: False if all tests pass, True if any test fails
    """
    failed = False

    # Create test directory
    test_dir = "test_rate_store_temp"
    if os.path.exists(test_dir):
        shutil.rmtree(test_dir)
    os.makedirs(test_dir)

    try:
        print("*** Test 1: Basic save and load")
        failed |= test_basic_save_load(os.path.join(test_dir, "test1"))

        print("*** Test 2: Frozen past slots")
        failed |= test_frozen_past_slots(os.path.join(test_dir, "test2"))

        print("*** Test 3: Future slots update")
        failed |= test_future_slots_update(os.path.join(test_dir, "test3"))

        print("*** Test 4: Cleanup old files")
        failed |= test_cleanup(os.path.join(test_dir, "test4"))

    finally:
        # Cleanup
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)

    return failed


def test_basic_save_load(test_dir):
    """Test basic save and load of rate tables"""

    os.makedirs(test_dir, exist_ok=True)

    class MockBase:
        def log(self, msg):
            print(f"  {msg}")

        def get_arg(self, key, default):
            if key == "rate_retention_days":
                return 7
            return default

    base = MockBase()
    store = RateStore(base, save_dir=test_dir)

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Create rate tables
    rate_import = {0: 10.0, 30: 15.0, 60: 20.0, 90: 25.0}
    rate_export = {0: 5.0, 30: 7.5, 60: 10.0, 90: 12.5}

    # Save rates with freeze at minute 60
    success = store.save_rates(today, rate_import, rate_export, freeze_before_minute=60)

    if not success:
        print("  ERROR: Failed to save rates")
        return True

    # Load rates back
    loaded_import, loaded_export = store.load_rates(today)

    if loaded_import is None or loaded_export is None:
        print("  ERROR: Failed to load rates")
        return True

    # Verify all rates loaded correctly
    for minute in rate_import:
        if minute not in loaded_import or abs(loaded_import[minute] - rate_import[minute]) > 0.01:
            print(f"  ERROR: Import rate mismatch at minute {minute}: expected {rate_import[minute]}, got {loaded_import.get(minute)}")
            return True

    for minute in rate_export:
        if minute not in loaded_export or abs(loaded_export[minute] - rate_export[minute]) > 0.01:
            print(f"  ERROR: Export rate mismatch at minute {minute}: expected {rate_export[minute]}, got {loaded_export.get(minute)}")
            return True

    print("  PASS: Basic save and load working")
    return False


def test_frozen_past_slots(test_dir):
    """Test that past slots (< freeze_before_minute) are frozen"""

    os.makedirs(test_dir, exist_ok=True)

    class MockBase:
        def log(self, msg):
            print(f"  {msg}")

        def get_arg(self, key, default):
            if key == "rate_retention_days":
                return 7
            return default

    base = MockBase()
    store = RateStore(base, save_dir=test_dir)

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Initial save with rates for minute 0-90
    rate_import_v1 = {0: 10.0, 30: 15.0, 60: 20.0, 90: 25.0}
    rate_export_v1 = {0: 5.0, 30: 7.5, 60: 10.0, 90: 12.5}

    store.save_rates(today, rate_import_v1, rate_export_v1, freeze_before_minute=60)

    # Second save with different rates - past slots (< 60) should be frozen
    rate_import_v2 = {0: 99.0, 30: 99.0, 60: 30.0, 90: 35.0}  # Changed all
    rate_export_v2 = {0: 99.0, 30: 99.0, 60: 15.0, 90: 17.5}

    store.save_rates(today, rate_import_v2, rate_export_v2, freeze_before_minute=60)

    # Load and verify
    loaded_import, loaded_export = store.load_rates(today)

    # Minutes 0 and 30 should still have original values (frozen)
    if abs(loaded_import[0] - 10.0) > 0.01:
        print(f"  ERROR: Frozen import rate at minute 0 changed from 10.0 to {loaded_import[0]}")
        return True

    if abs(loaded_import[30] - 15.0) > 0.01:
        print(f"  ERROR: Frozen import rate at minute 30 changed from 15.0 to {loaded_import[30]}")
        return True

    # Minutes 60 and 90 should have new values (not frozen)
    if abs(loaded_import[60] - 30.0) > 0.01:
        print(f"  ERROR: Future import rate at minute 60 not updated to 30.0, got {loaded_import[60]}")
        return True

    if abs(loaded_import[90] - 35.0) > 0.01:
        print(f"  ERROR: Future import rate at minute 90 not updated to 35.0, got {loaded_import[90]}")
        return True

    print("  PASS: Past slots correctly frozen")
    return False


def test_future_slots_update(test_dir):
    """Test that future slots (>= freeze_before_minute) always get new values"""

    os.makedirs(test_dir, exist_ok=True)

    class MockBase:
        def log(self, msg):
            print(f"  {msg}")

        def get_arg(self, key, default):
            if key == "rate_retention_days":
                return 7
            return default

    base = MockBase()
    store = RateStore(base, save_dir=test_dir)

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Save initial future rates
    rate_import_v1 = {120: 20.0, 150: 25.0}
    rate_export_v1 = {120: 10.0, 150: 12.5}

    store.save_rates(today, rate_import_v1, rate_export_v1, freeze_before_minute=90)

    # Update future rates multiple times
    for version in range(2, 5):
        rate_import_vN = {120: 20.0 + version * 10, 150: 25.0 + version * 10}
        rate_export_vN = {120: 10.0 + version * 5, 150: 12.5 + version * 5}
        store.save_rates(today, rate_import_vN, rate_export_vN, freeze_before_minute=90)

    # Load and verify we have the latest values
    loaded_import, loaded_export = store.load_rates(today)

    expected_import_120 = 20.0 + 4 * 10  # 60.0
    expected_import_150 = 25.0 + 4 * 10  # 65.0

    if abs(loaded_import[120] - expected_import_120) > 0.01:
        print(f"  ERROR: Future rate at 120 not updated to {expected_import_120}, got {loaded_import[120]}")
        return True

    if abs(loaded_import[150] - expected_import_150) > 0.01:
        print(f"  ERROR: Future rate at 150 not updated to {expected_import_150}, got {loaded_import[150]}")
        return True

    print("  PASS: Future slots always updated")
    return False


def test_cleanup(test_dir):
    """Test cleanup of old rate files"""

    os.makedirs(test_dir, exist_ok=True)

    class MockBase:
        def log(self, msg):
            print(f"  {msg}")

        def get_arg(self, key, default):
            if key == "rate_retention_days":
                return 7
            return default

    base = MockBase()
    store = RateStore(base, save_dir=test_dir)

    # Create rate files for last 10 days
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    for days_ago in range(10):
        old_date = today - timedelta(days=days_ago)
        date_str = old_date.strftime("%Y_%m_%d")
        file_path = os.path.join(test_dir, f"rates_{date_str}.json")

        # Write dummy data
        with open(file_path, "w") as f:
            json.dump({
                "rates_import": {"0": 10.0},
                "rates_export": {"0": 5.0},
                "last_updated": old_date.isoformat()
            }, f)

        # Set file modification time to match the date
        old_timestamp = (old_date - timedelta(hours=12)).timestamp()
        os.utime(file_path, (old_timestamp, old_timestamp))

    # Run cleanup with 7 days retention
    retention_days = 7
    removed = store.cleanup_old_files(retention_days)

    # Check remaining files
    remaining_files = [f for f in os.listdir(test_dir) if f.startswith("rates_") and f.endswith(".json") and not f.endswith(".bak")]

    # Should have at most 7 days + today = 8 files
    if len(remaining_files) > 8:
        print(f"  ERROR: Expected <= 8 files after cleanup, found {len(remaining_files)}")
        print(f"  Files: {sorted(remaining_files)}")
        return True

    # At least 2 files should be removed (days 8-9), possibly more depending on time of day
    if removed < 2:
        print(f"  ERROR: Expected to remove at least 2 files, removed {removed}")
        return True

    print("  PASS: Cleanup working correctly")
    return False
