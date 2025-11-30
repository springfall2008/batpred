# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timedelta, timezone


def test_format_time_ago(my_predbat):
    """
    Test the format_time_ago function from utils
    """
    failed = False
    print("**** Testing format_time_ago function ****")

    from utils import format_time_ago

    now = datetime.now(timezone.utc)

    # Test 1: None input returns "Never updated" (line 617)
    print("Test 1: None input returns 'Never updated'")
    result = format_time_ago(None)
    if result != "Never updated":
        print(f"ERROR: Test 1 failed - expected 'Never updated', got '{result}'")
        failed = True

    # Test 2: Just now (0 minutes ago) (line 631)
    print("Test 2: Just now (0 minutes)")
    result = format_time_ago(now)
    if result != "Just now":
        print(f"ERROR: Test 2 failed - expected 'Just now', got '{result}'")
        failed = True

    # Test 3: 1 minute ago (line 633)
    print("Test 3: 1 minute ago")
    one_min_ago = now - timedelta(minutes=1, seconds=30)
    result = format_time_ago(one_min_ago)
    if result != "1 minute ago":
        print(f"ERROR: Test 3 failed - expected '1 minute ago', got '{result}'")
        failed = True

    # Test 4: Multiple minutes ago (line 635)
    print("Test 4: Multiple minutes ago")
    five_min_ago = now - timedelta(minutes=5)
    result = format_time_ago(five_min_ago)
    if result != "5 minutes ago":
        print(f"ERROR: Test 4 failed - expected '5 minutes ago', got '{result}'")
        failed = True

    # Test 5: 45 minutes ago
    print("Test 5: 45 minutes ago")
    fortyfive_min_ago = now - timedelta(minutes=45)
    result = format_time_ago(fortyfive_min_ago)
    if result != "45 minutes ago":
        print(f"ERROR: Test 5 failed - expected '45 minutes ago', got '{result}'")
        failed = True

    # Test 6: 1 hour ago (60-119 minutes) (line 637)
    print("Test 6: 1 hour ago")
    one_hour_ago = now - timedelta(minutes=65)
    result = format_time_ago(one_hour_ago)
    if result != "1 hour ago":
        print(f"ERROR: Test 6 failed - expected '1 hour ago', got '{result}'")
        failed = True

    # Test 7: Multiple hours ago (lines 638-639)
    print("Test 7: Multiple hours ago")
    five_hours_ago = now - timedelta(hours=5)
    result = format_time_ago(five_hours_ago)
    if result != "5 hours ago":
        print(f"ERROR: Test 7 failed - expected '5 hours ago', got '{result}'")
        failed = True

    # Test 8: 1 day ago (line 643)
    print("Test 8: 1 day ago")
    one_day_ago = now - timedelta(days=1, hours=2)
    result = format_time_ago(one_day_ago)
    if result != "1 day ago":
        print(f"ERROR: Test 8 failed - expected '1 day ago', got '{result}'")
        failed = True

    # Test 9: Multiple days ago (line 645)
    print("Test 9: Multiple days ago")
    three_days_ago = now - timedelta(days=3)
    result = format_time_ago(three_days_ago)
    if result != "3 days ago":
        print(f"ERROR: Test 9 failed - expected '3 days ago', got '{result}'")
        failed = True

    # Test 10: Future time returns "Just now" (line 629)
    print("Test 10: Future time returns 'Just now'")
    future_time = now + timedelta(minutes=10)
    result = format_time_ago(future_time)
    if result != "Just now":
        print(f"ERROR: Test 10 failed - expected 'Just now' for future time, got '{result}'")
        failed = True

    print("**** format_time_ago tests completed ****")
    return failed
