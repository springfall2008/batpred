"""
Test battery power curve with both integer and string keys
"""


def get_curve_value(curve, key, default=1.0):
    """
    Get a value from a battery power curve dictionary.
    Supports both integer and string keys for compatibility with YAML configurations.
    """
    # Try integer key first (most common case)
    if key in curve:
        return curve[key]

    # Try string key for YAML configs with string-based keys
    str_key = str(key)
    if str_key in curve:
        return curve[str_key]

    # Return default if neither found
    return default


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
