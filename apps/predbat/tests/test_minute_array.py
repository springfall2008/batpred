# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from utils import MinuteArray


def test_minute_array(my_predbat):
    """
    Test the MinuteArray class from utils
    """
    failed = False
    print("**** Testing MinuteArray class ****")

    # Test 1: Construction from a dense dict
    print("Test 1: Construction from dense dict")
    data = {0: 1.0, 1: 2.0, 2: 3.0, 3: 4.0}
    ma = MinuteArray(data, 4)
    if len(ma) != 4:
        print(f"ERROR: Test 1 failed - expected len 4, got {len(ma)}")
        failed = True
    if ma[0] != 1.0 or ma[1] != 2.0 or ma[2] != 3.0 or ma[3] != 4.0:
        print(f"ERROR: Test 1 failed - unexpected values: {list(ma._data)}")
        failed = True

    # Test 2: Sparse dict — missing keys become 0.0
    print("Test 2: Sparse dict fills gaps with 0.0")
    sparse = {0: 5.0, 2: 7.0}
    ma2 = MinuteArray(sparse, 4)
    if ma2[1] != 0.0 or ma2[3] != 0.0:
        print(f"ERROR: Test 2 failed - expected 0.0 at missing keys, got {ma2[1]}, {ma2[3]}")
        failed = True
    if ma2[0] != 5.0 or ma2[2] != 7.0:
        print(f"ERROR: Test 2 failed - expected 5.0 and 7.0 at present keys, got {ma2[0]}, {ma2[2]}")
        failed = True

    # Test 3: get() within bounds, below bounds, above bounds, and with custom default
    print("Test 3: get() bounds and default")
    ma3 = MinuteArray({0: 10.0, 1: 20.0}, 2)
    if ma3.get(0) != 10.0:
        print(f"ERROR: Test 3a failed - expected 10.0, got {ma3.get(0)}")
        failed = True
    if ma3.get(2) != 0.0:
        print(f"ERROR: Test 3b failed - expected 0.0 for out-of-range, got {ma3.get(2)}")
        failed = True
    if ma3.get(-1) != 0.0:
        print(f"ERROR: Test 3c failed - expected 0.0 for negative key, got {ma3.get(-1)}")
        failed = True
    if ma3.get(99, 42.0) != 42.0:
        print(f"ERROR: Test 3d failed - expected custom default 42.0, got {ma3.get(99, 42.0)}")
        failed = True

    # Test 4: __contains__
    print("Test 4: __contains__")
    ma4 = MinuteArray({0: 1.0, 1: 2.0}, 2)
    if 0 not in ma4 or 1 not in ma4:
        print("ERROR: Test 4a failed - in-range keys not found")
        failed = True
    if 2 in ma4 or -1 in ma4:
        print("ERROR: Test 4b failed - out-of-range keys incorrectly found")
        failed = True

    # Test 5: __bool__
    print("Test 5: __bool__")
    ma5_full = MinuteArray({0: 0.0}, 1)
    ma5_empty = MinuteArray({}, 0)
    if not ma5_full:
        print("ERROR: Test 5a failed - non-empty array is falsy")
        failed = True
    if ma5_empty:
        print("ERROR: Test 5b failed - empty array is truthy")
        failed = True

    # Test 6: __setitem__
    print("Test 6: __setitem__")
    ma6 = MinuteArray({0: 1.0, 1: 2.0}, 2)
    ma6[0] = 99.0
    if ma6[0] != 99.0:
        print(f"ERROR: Test 6 failed - expected 99.0, got {ma6[0]}")
        failed = True

    # Test 7: __iter__ yields indices 0..len-1
    print("Test 7: __iter__")
    ma7 = MinuteArray({0: 10.0, 1: 20.0, 2: 30.0}, 3)
    indices = list(ma7)
    if indices != [0, 1, 2]:
        print(f"ERROR: Test 7 failed - expected [0, 1, 2], got {indices}")
        failed = True

    # Test 8: keys() returns range(0, len)
    print("Test 8: keys()")
    ma8 = MinuteArray({0: 1.0, 1: 2.0, 2: 3.0}, 3)
    if list(ma8.keys()) != [0, 1, 2]:
        print(f"ERROR: Test 8 failed - expected [0, 1, 2], got {list(ma8.keys())}")
        failed = True

    # Test 9: copy() returns an independent MinuteArray
    print("Test 9: copy() independence")
    ma9 = MinuteArray({0: 1.0, 1: 2.0}, 2)
    ma9_copy = ma9.copy()
    if not isinstance(ma9_copy, MinuteArray):
        print("ERROR: Test 9a failed - copy() did not return a MinuteArray")
        failed = True
    ma9_copy[0] = 99.0
    if ma9[0] != 1.0:
        print(f"ERROR: Test 9b failed - original was mutated by copy modification, got {ma9[0]}")
        failed = True
    if ma9_copy[0] != 99.0:
        print(f"ERROR: Test 9c failed - copy value not updated, got {ma9_copy[0]}")
        failed = True

    # Test 10: size larger than dict — extra entries are 0.0
    print("Test 10: Oversized MinuteArray pads with 0.0")
    ma10 = MinuteArray({0: 5.0}, 5)
    if len(ma10) != 5:
        print(f"ERROR: Test 10a failed - expected len 5, got {len(ma10)}")
        failed = True
    if ma10[4] != 0.0:
        print(f"ERROR: Test 10b failed - expected 0.0 at index 4, got {ma10[4]}")
        failed = True

    # Test 11: pad=False sizing — max(dict.keys()) + 2 captures all accumulated keys
    print("Test 11: pad=False size derived from max(dict.keys()) + 2")
    # Simulate two-entity accumulation: entity A adds keys 0..4, entity B adds keys 0..2
    # Final dict has keys 0..4 but age_days would reflect entity B (2 days → 2*1440 keys)
    big_dict = {i: float(i) for i in range(5)}
    size = max(big_dict.keys()) + 2  # 4 + 2 = 6
    ma11 = MinuteArray(big_dict, size)
    if len(ma11) != 6:
        print(f"ERROR: Test 11a failed - expected len 6, got {len(ma11)}")
        failed = True
    if ma11[4] != 4.0:
        print(f"ERROR: Test 11b failed - key 4 was truncated, got {ma11[4]}")
        failed = True
    if ma11[5] != 0.0:
        print(f"ERROR: Test 11c failed - expected 0.0 at padding index 5, got {ma11[5]}")
        failed = True

    # Test 12: integer values are coerced to float via __setitem__
    print("Test 12: __setitem__ coerces int to float")
    ma12 = MinuteArray({0: 0.0}, 1)
    ma12[0] = 7
    if ma12[0] != 7.0 or not isinstance(ma12[0], float):
        print(f"ERROR: Test 12 failed - expected 7.0 float, got {ma12[0]} ({type(ma12[0])})")
        failed = True

    if not failed:
        print("All MinuteArray tests passed")
    return failed
