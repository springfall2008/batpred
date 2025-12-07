"""
Test battery power curve with both integer and string keys
"""

from utils import get_curve_value

def run_battery_curve_keys_tests(my_predbat):
    """
    Run all battery curve key tests
    Returns 0 on success, 1 on failure
    """
    failed = 0
    
    try:
        test_get_curve_value_with_int_keys()
        test_get_curve_value_with_string_keys()
        test_get_curve_value_with_mixed_keys()
        test_get_curve_value_custom_default()
        print("**** Battery curve keys tests: All tests passed ****")
    except AssertionError as e:
        print(f"**** Battery curve keys tests FAILED: {e} ****")
        failed = 1
    except Exception as e:
        print(f"**** Battery curve keys tests FAILED with exception: {e} ****")
        failed = 1
    
    return failed



def test_get_curve_value_with_int_keys():
    """Test that get_curve_value works with integer keys (traditional format)"""
    curve = {100: 0.15, 99: 0.15, 98: 0.23, 97: 0.3}

    assert get_curve_value(curve, 100) == 0.15
    assert get_curve_value(curve, 99) == 0.15
    assert get_curve_value(curve, 98) == 0.23
    assert get_curve_value(curve, 97) == 0.3
    assert get_curve_value(curve, 96) == 1.0  # default value
    print("✓ Integer keys test passed")


def test_get_curve_value_with_string_keys():
    """Test that get_curve_value works with string keys (YAML/SOPS format)"""
    curve = {"100": 0.15, "99": 0.15, "98": 0.23, "97": 0.3}

    assert get_curve_value(curve, 100) == 0.15
    assert get_curve_value(curve, 99) == 0.15
    assert get_curve_value(curve, 98) == 0.23
    assert get_curve_value(curve, 97) == 0.3
    assert get_curve_value(curve, 96) == 1.0  # default value
    print("✓ String keys test passed")


def test_get_curve_value_with_mixed_keys():
    """Test that get_curve_value prioritizes integer keys over string keys"""
    # If both exist, integer key should be used
    curve = {100: 0.15, "99": 0.20}

    assert get_curve_value(curve, 100) == 0.15  # int key found
    assert get_curve_value(curve, 99) == 0.20  # string key found
    print("✓ Mixed keys test passed")


def test_get_curve_value_custom_default():
    """Test that custom default values work"""
    curve = {100: 0.15}

    assert get_curve_value(curve, 100) == 0.15
    assert get_curve_value(curve, 99, default=0.5) == 0.5
    assert get_curve_value(curve, 98, default=0.0) == 0.0
    print("✓ Custom default test passed")


if __name__ == "__main__":
    test_get_curve_value_with_int_keys()
    test_get_curve_value_with_string_keys()
    test_get_curve_value_with_mixed_keys()
    test_get_curve_value_custom_default()
    print("\n✅ All tests passed!")
