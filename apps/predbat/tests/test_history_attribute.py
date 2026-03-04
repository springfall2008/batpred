# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from utils import history_attribute


def test_history_attribute(my_predbat):
    """
    Test the history_attribute function from utils
    """
    failed = False
    print("**** Testing history_attribute function ****")

    # Test 1: Basic functionality
    print("Test 1: Basic functionality")
    history = [
        [
            {"state": "10.5", "last_updated": "2024-10-15T10:00:00+00:00"},
            {"state": "20.5", "last_updated": "2024-10-15T11:00:00+00:00"},
        ]
    ]
    result = history_attribute(history)
    if len(result) != 2:
        print(f"ERROR: Test 1 failed - expected 2 entries, got {len(result)}")
        failed = True
    else:
        values = list(result.values())
        if 10.5 not in values or 20.5 not in values:
            print(f"ERROR: Test 1 failed - expected values [10.5, 20.5], got {values}")
            failed = True

    # Test 2: Missing last_updated_key is skipped (line 85)
    print("Test 2: Missing last_updated_key is skipped")
    history = [
        [
            {"state": "10.5"},  # Missing last_updated
            {"state": "20.5", "last_updated": "2024-10-15T11:00:00+00:00"},
        ]
    ]
    result = history_attribute(history)
    if len(result) != 1:
        print(f"ERROR: Test 2 failed - expected 1 entry (skipped missing key), got {len(result)}")
        failed = True
    else:
        actual = list(result.values())[0]
        if actual != 20.5:
            print(f"ERROR: Test 2 failed - expected value 20.5, got {actual}")
            failed = True

    # Test 3: attributes=True mode (lines 88-90)
    print("Test 3: attributes=True mode")
    history = [
        [
            {"attributes": {"power": "15.0"}, "last_updated": "2024-10-15T10:00:00+00:00"},
            {"attributes": {"power": "25.0"}, "last_updated": "2024-10-15T11:00:00+00:00"},
        ]
    ]
    result = history_attribute(history, state_key="power", attributes=True)
    if len(result) != 2:
        print(f"ERROR: Test 3 failed - expected 2 entries from attributes, got {len(result)}")
        failed = True
    else:
        values = list(result.values())
        if 15.0 not in values or 25.0 not in values:
            print(f"ERROR: Test 3 failed - expected values [15.0, 25.0], got {values}")
            failed = True

    # Test 4: attributes=True with missing state_key in attributes (line 89)
    print("Test 4: attributes=True with missing state_key")
    history = [
        [
            {"attributes": {"other": "15.0"}, "last_updated": "2024-10-15T10:00:00+00:00"},
            {"attributes": {"power": "25.0"}, "last_updated": "2024-10-15T11:00:00+00:00"},
        ]
    ]
    result = history_attribute(history, state_key="power", attributes=True)
    if len(result) != 1:
        print(f"ERROR: Test 4 failed - expected 1 entry (skipped missing attr), got {len(result)}")
        failed = True
    else:
        actual = list(result.values())[0]
        if actual != 25.0:
            print(f"ERROR: Test 4 failed - expected value 25.0, got {actual}")
            failed = True

    # Test 5: Missing state_key is skipped (line 94)
    print("Test 5: Missing state_key is skipped")
    history = [
        [
            {"last_updated": "2024-10-15T10:00:00+00:00"},  # Missing state
            {"state": "20.5", "last_updated": "2024-10-15T11:00:00+00:00"},
        ]
    ]
    result = history_attribute(history)
    if len(result) != 1:
        print(f"ERROR: Test 5 failed - expected 1 entry (skipped missing state), got {len(result)}")
        failed = True
    else:
        actual = list(result.values())[0]
        if actual != 20.5:
            print(f"ERROR: Test 5 failed - expected value 20.5, got {actual}")
            failed = True

    # Test 6: unavailable/unknown values are skipped (line 98)
    print("Test 6: unavailable/unknown values are skipped")
    history = [
        [
            {"state": "unavailable", "last_updated": "2024-10-15T10:00:00+00:00"},
            {"state": "unknown", "last_updated": "2024-10-15T10:30:00+00:00"},
            {"state": "20.5", "last_updated": "2024-10-15T11:00:00+00:00"},
        ]
    ]
    result = history_attribute(history)
    if len(result) != 1:
        print(f"ERROR: Test 6 failed - expected 1 entry (skipped unavailable/unknown), got {len(result)}")
        failed = True
    else:
        actual = list(result.values())[0]
        if actual != 20.5:
            print(f"ERROR: Test 6 failed - expected value 20.5, got {actual}")
            failed = True

    # Test 7: pounds=True scaling (line 108)
    print("Test 7: pounds=True scaling")
    history = [
        [
            {"state": "1234", "last_updated": "2024-10-15T10:00:00+00:00"},  # 1234 pence = 12.34 pounds
        ]
    ]
    result = history_attribute(history, pounds=True)
    expected_value = 12.34
    if len(result) != 1:
        print(f"ERROR: Test 7 failed - expected 1 entry, got {len(result)}")
        failed = True
    else:
        actual = list(result.values())[0]
        if actual != expected_value:
            print(f"ERROR: Test 7 failed - expected {expected_value}, got {actual}")
            failed = True

    # Test 8: Boolean string conversion "on" -> 1 (lines 111-113)
    print("Test 8: Boolean string conversion 'on' -> 1")
    history = [
        [
            {"state": "on", "last_updated": "2024-10-15T10:00:00+00:00"},
            {"state": "true", "last_updated": "2024-10-15T10:30:00+00:00"},
            {"state": "yes", "last_updated": "2024-10-15T11:00:00+00:00"},
        ]
    ]
    result = history_attribute(history)
    if len(result) != 3:
        print(f"ERROR: Test 8 failed - expected 3 entries, got {len(result)}")
        failed = True
    else:
        for val in result.values():
            if val != 1:
                print(f"ERROR: Test 8 failed - expected all values to be 1, got {val}")
                failed = True
                break

    # Test 9: Boolean string conversion "off" -> 0 (lines 114-115)
    print("Test 9: Boolean string conversion 'off' -> 0")
    history = [
        [
            {"state": "off", "last_updated": "2024-10-15T10:00:00+00:00"},
            {"state": "false", "last_updated": "2024-10-15T10:30:00+00:00"},
            {"state": "no", "last_updated": "2024-10-15T11:00:00+00:00"},
        ]
    ]
    result = history_attribute(history)
    if len(result) != 3:
        print(f"ERROR: Test 9 failed - expected 3 entries, got {len(result)}")
        failed = True
    else:
        for val in result.values():
            if val != 0:
                print(f"ERROR: Test 9 failed - expected all values to be 0, got {val}")
                failed = True
                break

    # Test 10: Unknown string is skipped (line 117)
    print("Test 10: Unknown string values are skipped")
    history = [
        [
            {"state": "some_random_string", "last_updated": "2024-10-15T10:00:00+00:00"},
            {"state": "20.5", "last_updated": "2024-10-15T11:00:00+00:00"},
        ]
    ]
    result = history_attribute(history)
    if len(result) != 1:
        print(f"ERROR: Test 10 failed - expected 1 entry (skipped unknown string), got {len(result)}")
        failed = True
    else:
        actual = list(result.values())[0]
        if actual != 20.5:
            print(f"ERROR: Test 10 failed - expected value 20.5, got {actual}")
            failed = True

    # Test 11: daily=True mode (line 138)
    print("Test 11: daily=True mode")
    history = [
        [
            {"state": "10.5", "last_updated": "2024-10-15T10:00:00+00:00"},
            {"state": "20.5", "last_updated": "2024-10-15T15:00:00+00:00"},  # Same day
            {"state": "30.5", "last_updated": "2024-10-16T10:00:00+00:00"},  # Different day
        ]
    ]
    result = history_attribute(history, daily=True, first=True)
    # With first=True and daily=True, should only keep first entry per day
    if len(result) != 2:
        print(f"ERROR: Test 11 failed - expected 2 entries (one per day with first=True), got {len(result)}")
        failed = True
    else:
        # With first=True, should keep first entry per day (10.5 for Oct 15, 30.5 for Oct 16)
        values = list(result.values())
        if 10.5 not in values:
            print(f"ERROR: Test 11 failed - expected 10.5 (first entry for Oct 15), got {values}")
            failed = True
        if 30.5 not in values:
            print(f"ERROR: Test 11 failed - expected 30.5 (first entry for Oct 16), got {values}")
            failed = True

    # Test 12: daily=False mode (line 140)
    print("Test 12: daily=False mode")
    history = [
        [
            {"state": "10.5", "last_updated": "2024-10-15T10:00:00+00:00"},
            {"state": "20.5", "last_updated": "2024-10-15T15:00:00+00:00"},
        ]
    ]
    result = history_attribute(history, daily=False)
    if len(result) != 2:
        print(f"ERROR: Test 12 failed - expected 2 entries (all kept with daily=False), got {len(result)}")
        failed = True
    else:
        # Verify both values are present
        values = list(result.values())
        if 10.5 not in values or 20.5 not in values:
            print(f"ERROR: Test 12 failed - expected [10.5, 20.5], got {values}")
            failed = True
        # Verify keys are the original timestamps (not daily format)
        keys = list(result.keys())
        if "2024-10-15T10:00:00+00:00" not in keys or "2024-10-15T15:00:00+00:00" not in keys:
            print(f"ERROR: Test 12 failed - expected original timestamps as keys, got {keys}")
            failed = True

    # Test 13: Invalid timestamp causes skip (lines 124-125)
    print("Test 13: Invalid timestamp causes skip")
    history = [
        [
            {"state": "10.5", "last_updated": "invalid-timestamp"},
            {"state": "20.5", "last_updated": "2024-10-15T11:00:00+00:00"},
        ]
    ]
    result = history_attribute(history)
    if len(result) != 1:
        print(f"ERROR: Test 13 failed - expected 1 entry (skipped invalid timestamp), got {len(result)}")
        failed = True
    else:
        # Verify the valid entry was kept with correct value
        actual_value = list(result.values())[0]
        if actual_value != 20.5:
            print(f"ERROR: Test 13 failed - expected value 20.5, got {actual_value}")
            failed = True

    # Test 14: Non-list input returns empty dict
    print("Test 14: Non-list input returns empty dict")
    result = history_attribute("not a list")
    if result != {}:
        print(f"ERROR: Test 14 failed - expected empty dict for non-list input, got {result}")
        failed = True

    # Test 15: offset_days parameter
    print("Test 15: offset_days parameter")
    history = [
        [
            {"state": "10.5", "last_updated": "2024-10-15T10:00:00+00:00"},
        ]
    ]
    result = history_attribute(history, daily=True, offset_days=1)
    if len(result) != 1:
        print(f"ERROR: Test 15 failed - expected 1 entry with offset_days, got {len(result)}")
        failed = True

    # Test 16: scale parameter
    print("Test 16: scale parameter")
    history = [
        [
            {"state": "10.0", "last_updated": "2024-10-15T10:00:00+00:00"},
        ]
    ]
    result = history_attribute(history, scale=2.0)
    if len(result) != 1:
        print(f"ERROR: Test 16 failed - expected 1 entry, got {len(result)}")
        failed = True
    else:
        actual = list(result.values())[0]
        if actual != 20.0:
            print(f"ERROR: Test 16 failed - expected 20.0 with scale=2.0, got {actual}")
            failed = True

    print("**** history_attribute tests completed ****")
    return failed
