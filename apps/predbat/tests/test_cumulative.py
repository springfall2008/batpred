# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from utils import get_now_from_cumulative


def test_get_now_from_cumulative(my_predbat):
    """
    Test the get_now_from_cumulative function from utils
    """
    failed = False
    print("**** Testing get_now_from_cumulative function ****")

    # Test 1: backwards=True - normal data
    print("Test 1: backwards=True with normal data")
    data = {0: 100, 10: 90, 15: 85, 20: 80, 25: 75, 30: 70}
    minutes_now = 15
    result = get_now_from_cumulative(data, minutes_now, backwards=True)
    # backwards: lowest in range 15-10 is min(85,90) = 85, value = data[0] - lowest = 100 - 85 = 15
    expected = 15
    if result != expected:
        print(f"ERROR: Test 1 failed - expected {expected} got {result}")
        failed = True

    # Test 2: backwards=False - normal data
    print("Test 2: backwards=False with normal data")
    data = {0: 10, 1: 12, 2: 14, 3: 16, 4: 18, 10: 30, 15: 45}
    minutes_now = 15
    result = get_now_from_cumulative(data, minutes_now, backwards=False)
    # forwards: lowest in range 0-4 is min(10,12,14,16,18) = 10, value = data[15] - lowest = 45 - 10 = 35
    expected = 35
    if result != expected:
        print(f"ERROR: Test 2 failed - expected {expected} got {result}")
        failed = True

    # Test 3: backwards=True with missing keys in lookup range
    print("Test 3: backwards=True with missing keys")
    data = {0: 100, 15: 85}  # Missing 11,12,13,14
    minutes_now = 15
    result = get_now_from_cumulative(data, minutes_now, backwards=True)
    # lowest = min(data.get(15,inf), data.get(14,inf), data.get(13,inf), data.get(12,inf), data.get(11,inf)) = 85
    expected = 15  # 100 - 85
    if result != expected:
        print(f"ERROR: Test 3 failed - expected {expected} got {result}")
        failed = True

    # Test 4: backwards=False with missing keys at start
    print("Test 4: backwards=False with missing keys at start")
    data = {5: 50, 10: 100}  # Missing 0,1,2,3,4
    minutes_now = 10
    result = get_now_from_cumulative(data, minutes_now, backwards=False)
    # lowest from range 0-4 all missing so lowest stays 9999999999, value = data.get(10,0) - lowest = 100 - 9999999999 < 0, max(value,0) = 0
    expected = 0
    if result != expected:
        print(f"ERROR: Test 4 failed - expected {expected} got {result}")
        failed = True

    # Test 5: Empty data returns 0
    print("Test 5: Empty data")
    data = {}
    result = get_now_from_cumulative(data, 10, backwards=True)
    expected = 0  # max(0 - 9999999999, 0) = 0
    if result != expected:
        print(f"ERROR: Test 5 failed - expected {expected} got {result}")
        failed = True

    # Test 6: backwards=True at minute 0
    print("Test 6: backwards=True at minute 0")
    data = {0: 50, 1: 45, 2: 40}
    minutes_now = 0
    result = get_now_from_cumulative(data, minutes_now, backwards=True)
    # Range is 0 to -4 (but negative indices won't be in data), so lowest is data.get(0) = 50
    expected = 0  # data[0] - lowest = 50 - 50 = 0
    if result != expected:
        print(f"ERROR: Test 6 failed - expected {expected} got {result}")
        failed = True

    print("**** get_now_from_cumulative tests completed ****")
    return failed
