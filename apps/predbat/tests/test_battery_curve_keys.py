"""
Test battery power curve with both integer and string keys
"""

from utils import get_curve_value


def run_battery_curve_keys_tests(my_predbat):
    """
    Run all battery curve key tests
    Returns False on success, True on failure
    """
    failed = False

    try:
        test_get_curve_value_with_int_keys(my_predbat)
        test_get_curve_value_with_string_keys(my_predbat)
        test_get_curve_value_with_mixed_keys(my_predbat)
        test_get_curve_value_custom_default(my_predbat)
        print("**** Battery curve keys tests: All tests passed ****")
    except AssertionError as e:
        print(f"**** Battery curve keys tests FAILED: {e} ****")
        failed = True
    except Exception as e:
        print(f"**** Battery curve keys tests FAILED with exception: {e} ****")
        failed = True

    return failed


def test_get_curve_value_with_int_keys(my_predbat):
    """Test that get_curve_value works with integer keys (traditional format)"""
    curve = {100: 0.15, 99: 0.15, 98: 0.23, 97: 0.3}
    curve = my_predbat.validate_curve(curve, "test_int_keys_curve")

    assert get_curve_value(curve, 100) == 0.15
    assert get_curve_value(curve, 99) == 0.15
    assert get_curve_value(curve, 98) == 0.23
    assert get_curve_value(curve, 97) == 0.3
    assert get_curve_value(curve, 96) == 1.0  # default value
    print("✓ Integer keys test passed")


def test_get_curve_value_with_string_keys(my_predbat):
    """Test that get_curve_value works with string keys (YAML/SOPS format)"""
    curve = {"100": 0.15, "99": 0.15, "98": 0.23, "97": 0.3}
    curve = my_predbat.validate_curve(curve, "test_string_keys_curve")

    assert get_curve_value(curve, 100) == 0.15
    assert get_curve_value(curve, 99) == 0.15
    assert get_curve_value(curve, 98) == 0.23
    assert get_curve_value(curve, 97) == 0.3
    assert get_curve_value(curve, 96) == 1.0  # default value
    print("✓ String keys test passed")


def test_get_curve_value_with_mixed_keys(my_predbat):
    """Test that get_curve_value prioritizes integer keys over string keys"""
    # If both exist, integer key should be used
    curve = {100: 0.15, "99": 0.20}
    curve = my_predbat.validate_curve(curve, "test_mixed_keys_curve")

    assert get_curve_value(curve, 100) == 0.15  # int key found
    assert get_curve_value(curve, 99) == 0.20  # string key found
    print("✓ Mixed keys test passed")


def test_get_curve_value_custom_default(my_predbat):
    """Test that custom default values work"""
    curve = {100: 0.15}
    curve = my_predbat.validate_curve(curve, "test_custom_default_curve")

    assert get_curve_value(curve, 100) == 0.15
    assert get_curve_value(curve, 99, default=0.5) == 0.5
    assert get_curve_value(curve, 98, default=0.0) == 0.0
    print("✓ Custom default test passed")


if __name__ == "__main__":
    # Note: When running standalone, validate_curve is not available
    # This is meant to be run through the test framework with my_predbat instance
    print("This test must be run through the test framework with my_predbat instance")
    print("Use: ./run_all --test test_battery_curve_keys")
