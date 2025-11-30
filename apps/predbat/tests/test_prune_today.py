# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import pytz
from datetime import datetime
from utils import prune_today

def test_prune_today(my_predbat):
    """
    Test the prune_today function from utils
    """
    failed = False
    print("**** Testing prune_today function ****")


    utc = pytz.UTC
    now_utc = datetime(2024, 10, 15, 14, 30, 0, tzinfo=utc)
    midnight_utc = datetime(2024, 10, 15, 0, 0, 0, tzinfo=utc)

    # Test 1: prune=True removes data before midnight
    print("Test 1: prune=True removes data before midnight")
    data = {
        "2024-10-14T23:00:00+00:00": 10,  # Before midnight - should be pruned
        "2024-10-15T01:00:00+00:00": 20,  # After midnight - should be kept
        "2024-10-15T10:00:00+00:00": 30,  # After midnight - should be kept
    }
    result = prune_today(data, now_utc, midnight_utc, prune=True, group=15)
    if "2024-10-14T23:00:00+00:00" in result:
        print("ERROR: Test 1 failed - data before midnight should be pruned")
        failed = True
    if "2024-10-15T01:00:00+00:00" not in result or "2024-10-15T10:00:00+00:00" not in result:
        print("ERROR: Test 1 failed - data after midnight should be kept")
        failed = True

    # Test 2: prune=False keeps all data
    print("Test 2: prune=False keeps all data")
    result = prune_today(data, now_utc, midnight_utc, prune=False, group=15)
    if len(result) != 3:
        print(f"ERROR: Test 2 failed - expected 3 entries, got {len(result)}")
        failed = True

    # Test 3: group parameter filters close timestamps
    print("Test 3: group parameter filters close timestamps")
    data = {
        "2024-10-15T10:00:00+00:00": 10,
        "2024-10-15T10:05:00+00:00": 15,  # Within 15 min of previous - should be skipped
        "2024-10-15T10:20:00+00:00": 20,  # More than 15 min from first - should be kept
    }
    result = prune_today(data, now_utc, midnight_utc, prune=False, group=15)
    if len(result) != 2:
        print(f"ERROR: Test 3 failed - expected 2 entries (grouped), got {len(result)}")
        failed = True

    # Test 4: prune_future=True removes future data
    print("Test 4: prune_future=True removes future data")
    data = {
        "2024-10-15T10:00:00+00:00": 10,  # Past - should be kept
        "2024-10-15T16:00:00+00:00": 20,  # Future - should be pruned
    }
    result = prune_today(data, now_utc, midnight_utc, prune=False, group=15, prune_future=True)
    if "2024-10-15T16:00:00+00:00" in result:
        print("ERROR: Test 4 failed - future data should be pruned")
        failed = True
    if "2024-10-15T10:00:00+00:00" not in result:
        print("ERROR: Test 4 failed - past data should be kept")
        failed = True

    # Test 5: intermediate=True adds data points in gaps
    print("Test 5: intermediate=True adds intermediate data points")
    data = {
        "2024-10-15T10:00:00+00:00": 10,
        "2024-10-15T11:00:00+00:00": 20,  # 60 min gap > 15 min group
    }
    result = prune_today(data, now_utc, midnight_utc, prune=False, group=15, intermediate=True)
    # Should have original 2 entries + intermediate entries (60/15 - 1 = 3 intermediate points)
    if len(result) < 4:
        print(f"ERROR: Test 5 failed - expected at least 4 entries with intermediates, got {len(result)}")
        failed = True
    else:
        # Check original values are preserved
        if result.get("2024-10-15T10:00:00+00:00") != 10:
            print(f"ERROR: Test 5 failed - expected value 10 at 10:00, got {result.get('2024-10-15T10:00:00+00:00')}")
            failed = True
        if result.get("2024-10-15T11:00:00+00:00") != 20:
            print(f"ERROR: Test 5 failed - expected value 20 at 11:00, got {result.get('2024-10-15T11:00:00+00:00')}")
            failed = True

    # Test 6: TIME_FORMAT_SECONDS format (with microseconds)
    print("Test 6: TIME_FORMAT_SECONDS format")
    data = {
        "2024-10-15T10:00:00.123456+00:00": 10,
        "2024-10-15T11:00:00.654321+00:00": 20,
    }
    result = prune_today(data, now_utc, midnight_utc, prune=False, group=15)
    if len(result) != 2:
        print(f"ERROR: Test 6 failed - expected 2 entries, got {len(result)}")
        failed = True
    else:
        # Check values are preserved with microsecond timestamps
        values = list(result.values())
        if 10 not in values or 20 not in values:
            print(f"ERROR: Test 6 failed - expected values [10, 20], got {values}")
            failed = True

    print("**** prune_today tests completed ****")
    return failed