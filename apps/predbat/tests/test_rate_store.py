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
    Run comprehensive tests for rate persistence and finalisation

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
        print("*** Test 1: Basic rate persistence")
        failed |= test_basic_persistence(os.path.join(test_dir, "test1"))

        print("*** Test 2: Rate finalisation")
        failed |= test_finalisation(os.path.join(test_dir, "test2"))

        print("*** Test 3: Override priority (manual > automatic > initial)")
        failed |= test_override_priority(os.path.join(test_dir, "test3"))

        print("*** Test 4: Finalised rates resist fresh API data")
        failed |= test_finalised_resistance(os.path.join(test_dir, "test4"))

        print("*** Test 5: Cleanup old files")
        failed |= test_cleanup(os.path.join(test_dir, "test5"))

    finally:
        # Cleanup
        if os.path.exists(test_dir):
            shutil.rmtree(test_dir)

    return failed


def test_basic_persistence(test_dir):
    """Test basic write and read of rates"""

    # Create test subdirectory
    os.makedirs(test_dir, exist_ok=True)

    # Create mock base object
    class MockBase:
        def __init__(self):
            self.plan_interval_minutes = 30
            self.minutes_now = 720  # 12:00

        def log(self, msg):
            print(f"  {msg}")

        def get_arg(self, key, default):
            if key == "rate_retention_days":
                return 7
            return default

    base = MockBase()
    store = RateStore(base, save_dir=test_dir)

    # Write some base rates
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    for hour in range(24):
        minute = hour * 60
        import_rate = 10.0 + hour  # Rates from 10.0 to 33.0
        export_rate = 5.0 + hour   # Rates from 5.0 to 28.0
        store.write_base_rate(today, minute, import_rate, export_rate)

    # Verify rates written
    for hour in range(24):
        minute = hour * 60
        expected_import = 10.0 + hour
        expected_export = 5.0 + hour

        actual_import = store.get_rate(today, minute, is_import=True)
        actual_export = store.get_rate(today, minute, is_import=False)

        if actual_import is None or abs(actual_import - expected_import) > 0.01:
            print(f"  ERROR: Expected import rate {expected_import} at minute {minute}, got {actual_import}")
            return True

        if actual_export is None or abs(actual_export - expected_export) > 0.01:
            print(f"  ERROR: Expected export rate {expected_export} at minute {minute}, got {actual_export}")
            return True

    print("  PASS: Basic persistence working")
    return False


def test_finalisation(test_dir):
    """Test that rates become finalised after their slot time + buffer"""

    # Create test subdirectory
    os.makedirs(test_dir, exist_ok=True)

    class MockBase:
        def __init__(self):
            self.plan_interval_minutes = 30
            self.minutes_now = 720  # 12:00

        def log(self, msg):
            print(f"  {msg}")

        def get_arg(self, key, default):
            if key == "rate_retention_days":
                return 7
            return default

    base = MockBase()
    store = RateStore(base, save_dir=test_dir)

    # Write base rates for past slots (more than 5 minutes ago)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Write rates for slots that should be finalised (00:00, 00:30, 01:00)
    store.write_base_rate(today, 0, 15.0, 5.0)
    store.write_base_rate(today, 30, 20.0, 10.0)
    store.write_base_rate(today, 60, 25.0, 15.0)

    # Finalise past slots (set current minute to 70 which is past 01:00+5min buffer)
    store.finalise_slots(today, 70)

    # Check that slots are finalized in the JSON file
    date_str = today.strftime("%Y_%m_%d")
    file_path = os.path.join(test_dir, f"rates_{date_str}.json")

    if not os.path.exists(file_path):
        print(f"  ERROR: Rate file not found at {file_path}")
        return True

    with open(file_path, "r") as f:
        data = json.load(f)

    # Check finalised flags
    if "rates_import" not in data or "rates_export" not in data:
        print("  ERROR: Missing rate sections in file")
        return True

    # Slot at 0 should be finalised
    if "00:00" not in data["rates_import"] or not data["rates_import"]["00:00"]["finalised"]:
        print("  ERROR: Slot 00:00 import should be finalised")
        return True

    if "00:00" not in data["rates_export"] or not data["rates_export"]["00:00"]["finalised"]:
        print("  ERROR: Slot 00:00 export should be finalised")
        return True

    # Slot at 30 should be finalised
    if "00:30" not in data["rates_import"] or not data["rates_import"]["00:30"]["finalised"]:
        print("  ERROR: Slot 00:30 import should be finalised")
        return True

    # Slot at 60 should be finalised
    if "01:00" not in data["rates_import"] or not data["rates_import"]["01:00"]["finalised"]:
        print("  ERROR: Slot 01:00 import should be finalised")
        return True

    print("  PASS: Finalisation working correctly")
    return False


def test_override_priority(test_dir):
    """Test that manual overrides take priority over automatic, which take priority over initial"""

    # Create test subdirectory
    os.makedirs(test_dir, exist_ok=True)

    class MockBase:
        def __init__(self):
            self.plan_interval_minutes = 30
            self.minutes_now = 720  # 12:00

        def log(self, msg):
            print(f"  {msg}")

        def get_arg(self, key, default):
            if key == "rate_retention_days":
                return 7
            return default

    base = MockBase()
    store = RateStore(base, save_dir=test_dir)

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    minute = 120  # 02:00

    # Write initial rate
    store.write_base_rate(today, minute, 10.0, 5.0)

    # Check initial rate
    rate = store.get_rate(today, minute, is_import=True)
    if rate is None or abs(rate - 10.0) > 0.01:
        print(f"  ERROR: Expected initial import rate 10.0, got {rate}")
        return True

    # Apply automatic override (IOG)
    store.update_auto_override(today, minute, 5.0, 2.0, source="IOG")

    rate = store.get_rate(today, minute, is_import=True)
    if rate is None or abs(rate - 5.0) > 0.01:
        print(f"  ERROR: Expected automatic import rate 5.0, got {rate}")
        return True

    # Check automatic rate directly
    auto_rate = store.get_automatic_rate(today, minute, is_import=True)
    if auto_rate is None or abs(auto_rate - 5.0) > 0.01:
        print(f"  ERROR: Expected get_automatic_rate 5.0, got {auto_rate}")
        return True

    # Apply manual override
    store.update_manual_override(today, minute, 3.0, 1.0)

    rate = store.get_rate(today, minute, is_import=True)
    if rate is None or abs(rate - 3.0) > 0.01:
        print(f"  ERROR: Expected manual import rate 3.0, got {rate}")
        return True

    # Check automatic rate is still preserved
    auto_rate = store.get_automatic_rate(today, minute, is_import=True)
    if auto_rate is None or abs(auto_rate - 5.0) > 0.01:
        print(f"  ERROR: Automatic rate should still be 5.0, got {auto_rate}")
        return True

    print("  PASS: Override priority working correctly")
    return False


def test_finalised_resistance(test_dir):
    """Test that finalised rates resist new API data"""

    # Create test subdirectory
    os.makedirs(test_dir, exist_ok=True)

    class MockBase:
        def __init__(self):
            self.plan_interval_minutes = 30
            self.minutes_now = 720  # 12:00

        def log(self, msg):
            print(f"  {msg}")

        def get_arg(self, key, default):
            if key == "rate_retention_days":
                return 7
            return default

    base = MockBase()
    store = RateStore(base, save_dir=test_dir)

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    minute = 0  # 00:00

    # Write initial rate
    store.write_base_rate(today, minute, 15.0, 5.0)

    # Finalise it (minute 10 is past minute 0 + 5 minute buffer)
    store.finalise_slots(today, 10)

    # Try to overwrite with new API data
    store.write_base_rate(today, minute, 25.0, 10.0)

    # Should still be 15.0 (finalised rate resists changes)
    rate = store.get_rate(today, minute, is_import=True)
    if rate is None or abs(rate - 15.0) > 0.01:
        print(f"  ERROR: Finalised import rate changed from 15.0 to {rate}")
        return True

    # Export should also resist
    export_rate = store.get_rate(today, minute, is_import=False)
    if export_rate is None or abs(export_rate - 5.0) > 0.01:
        print(f"  ERROR: Finalised export rate changed from 5.0 to {export_rate}")
        return True

    print("  PASS: Finalised rates resist new API data")
    return False


def test_cleanup(test_dir):
    """Test cleanup of old rate files"""

    # Create test subdirectory
    os.makedirs(test_dir, exist_ok=True)

    class MockBase:
        def __init__(self):
            self.plan_interval_minutes = 30
            self.minutes_now = 720  # 12:00

        def log(self, msg):
            print(f"  {msg}")

        def get_arg(self, key, default):
            if key == "rate_retention_days":
                return 7
            return default

    base = MockBase()
    store = RateStore(base, save_dir=test_dir)

    # Create some old rate files manually
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    for days_ago in range(10):
        old_date = today - timedelta(days=days_ago)
        date_str = old_date.strftime("%Y_%m_%d")
        file_path = os.path.join(test_dir, f"rates_{date_str}.json")

        # Write dummy data
        with open(file_path, "w") as f:
            json.dump({
                "rates_import": {},
                "rates_export": {},
                "last_updated": old_date.isoformat()
            }, f)

        # Set file modification time to match the date (so cleanup works correctly)
        old_timestamp = (old_date - timedelta(hours=12)).timestamp()  # Set to noon of that day
        os.utime(file_path, (old_timestamp, old_timestamp))

    # Run cleanup with 7 days retention
    retention_days = 7
    store.cleanup_old_files(retention_days)

    # Check files - should have at most retention_days + 1 (today) files
    remaining_files = [f for f in os.listdir(test_dir) if f.startswith("rates_") and f.endswith(".json") and not f.endswith(".bak")]

    # Should have at most 7 days of retention + today = 8 files
    if len(remaining_files) > 8:
        print(f"  ERROR: Expected <= 8 files after cleanup, found {len(remaining_files)}")
        print(f"  Files: {sorted(remaining_files)}")
        return True

    print("  PASS: Cleanup working correctly")
    return False
