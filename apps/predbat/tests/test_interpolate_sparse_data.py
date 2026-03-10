"""
Test interpolate_sparse_data function from utils.py

Tests cover:
- Empty data
- Single data point
- Two data points
- Already-dense data (every minute populated)
- Sparse 5-minute interval data (the SaaS scenario)
- Energy preservation after interpolation
- Midnight reset detection (large value drops are not interpolated across)
- Mixed gap sizes
- Large datasets (full day of 5-minute data)
"""

import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from utils import interpolate_sparse_data


def test_empty_data():
    """Empty dict should return empty dict"""
    print("\n=== Test: Empty data ===")
    result = interpolate_sparse_data({})
    assert result == {}, f"Expected empty dict, got {result}"
    print("PASSED")


def test_single_point():
    """Single data point should return unchanged"""
    print("\n=== Test: Single point ===")
    data = {5: 3.0}
    result = interpolate_sparse_data(data)
    assert result == {5: 3.0}, f"Expected {{5: 3.0}}, got {result}"
    print("PASSED")


def test_two_points_adjacent():
    """Two adjacent points (gap=1) should return unchanged"""
    print("\n=== Test: Two adjacent points ===")
    data = {0: 10.0, 1: 9.5}
    result = interpolate_sparse_data(data)
    assert len(result) == 2, f"Expected 2 entries, got {len(result)}"
    assert result[0] == 10.0
    assert result[1] == 9.5
    print("PASSED")


def test_two_points_with_gap():
    """Two points with a gap should be linearly interpolated"""
    print("\n=== Test: Two points with gap ===")
    data = {0: 10.0, 4: 6.0}
    result = interpolate_sparse_data(data)
    assert len(result) == 5, f"Expected 5 entries, got {len(result)}"
    assert result[0] == 10.0
    assert abs(result[1] - 9.0) < 0.001, f"Expected 9.0, got {result[1]}"
    assert abs(result[2] - 8.0) < 0.001, f"Expected 8.0, got {result[2]}"
    assert abs(result[3] - 7.0) < 0.001, f"Expected 7.0, got {result[3]}"
    assert result[4] == 6.0
    print("PASSED")


def test_already_dense():
    """Already-dense data should pass through unchanged"""
    print("\n=== Test: Already dense data ===")
    data = {i: 10.0 - i * 0.1 for i in range(20)}
    result = interpolate_sparse_data(data)
    assert len(result) == 20, f"Expected 20 entries, got {len(result)}"
    for k in data:
        assert abs(result[k] - data[k]) < 0.0001, f"Key {k}: expected {data[k]}, got {result[k]}"
    print("PASSED")


def test_sparse_5min_intervals():
    """
    Simulate the SaaS scenario: cumulative load data at 5-minute intervals.
    Should produce dense output with every minute filled.
    """
    print("\n=== Test: Sparse 5-minute intervals (SaaS scenario) ===")
    # Simulate 60 minutes of cumulative decreasing load data at 5-min intervals
    # (backwards in time: minute 0 is now with highest value)
    sparse = {}
    for i in range(0, 65, 5):
        sparse[i] = 10.0 - (i / 60.0) * 3.0  # Decreasing from 10.0 to 6.8 over 60 min

    result = interpolate_sparse_data(sparse)

    # Should have entries for every minute from 0 to 60
    for m in range(0, 61):
        assert m in result, f"Missing minute {m}"
    assert len(result) >= 61, f"Expected at least 61 entries, got {len(result)}"

    # Values should decrease monotonically
    for m in range(1, 61):
        assert result[m] <= result[m - 1], f"Not monotonically decreasing at minute {m}: {result[m]} > {result[m-1]}"

    # Check specific interpolated values
    # Between minute 0 (10.0) and minute 5 (9.75), minute 2 should be ~9.9
    expected_m2 = 10.0 + (9.75 - 10.0) * (2 / 5)
    assert abs(result[2] - expected_m2) < 0.001, f"Minute 2: expected {expected_m2}, got {result[2]}"

    print(f"  Input: {len(sparse)} points -> Output: {len(result)} points")
    print("PASSED")


def test_energy_preservation():
    """
    Total energy (difference between first and last value) must be preserved
    after interpolation.
    """
    print("\n=== Test: Energy preservation ===")
    # Sparse data: cumulative load decreasing from 20.0 to 15.0 at 5-min intervals
    sparse = {0: 20.0, 5: 19.0, 10: 18.0, 15: 17.5, 20: 16.0, 25: 15.0}
    result = interpolate_sparse_data(sparse)

    original_energy = sparse[0] - sparse[25]
    interpolated_energy = result[0] - result[25]

    assert abs(original_energy - interpolated_energy) < 0.0001, f"Energy not preserved: original={original_energy}, interpolated={interpolated_energy}"

    # Also check that per-minute deltas sum correctly
    delta_sum = sum(result[m] - result[m + 1] for m in range(25))
    assert abs(delta_sum - original_energy) < 0.001, f"Per-minute delta sum {delta_sum} doesn't match total energy {original_energy}"

    print(f"  Original energy: {original_energy} kWh, Interpolated: {interpolated_energy} kWh")
    print("PASSED")


def test_midnight_reset_not_interpolated():
    """
    When cumulative value drops by >50% (midnight reset), interpolation should
    carry the previous value forward rather than interpolating through the drop.
    """
    print("\n=== Test: Midnight reset detection ===")
    # Simulate: values increasing then reset at midnight
    # Minute 0: 10.0, Minute 5: 8.0, Minute 10: 1.0 (reset!), Minute 15: 0.5
    data = {0: 10.0, 5: 8.0, 10: 1.0, 15: 0.5}

    result = interpolate_sparse_data(data)

    # Between minutes 5 and 10 there's a drop from 8.0 to 1.0
    # That's a drop of 7.0, which is >50% of 8.0 (4.0), so it's a reset
    # Minutes 6-9 should be carried forward at 8.0
    for m in range(6, 10):
        assert abs(result[m] - 8.0) < 0.001, f"Minute {m}: expected 8.0 (carry forward across reset), got {result[m]}"

    # Between minutes 0 and 5 there's a normal drop from 10.0 to 8.0
    # That's only 2.0, which is <50% of 10.0 (5.0), so interpolation should occur
    assert abs(result[2] - 9.2) < 0.001, f"Minute 2: expected 9.2 (interpolated), got {result[2]}"

    # Between minutes 10 and 15 there's a drop from 1.0 to 0.5
    # That's 0.5, which is exactly 50% of 1.0 - should NOT trigger reset (> not >=)
    assert abs(result[12] - 0.8) < 0.001, f"Minute 12: expected 0.8 (interpolated, 50% is not > 50%), got {result[12]}"

    print("PASSED")


def test_no_reset_for_small_drops():
    """
    Normal decreasing cumulative values (less than 50% drop) should be interpolated normally.
    """
    print("\n=== Test: No false reset for small drops ===")
    # Drop of 40% should NOT trigger reset detection
    data = {0: 10.0, 5: 6.0}  # Drop of 4.0, which is 40% of 10.0
    result = interpolate_sparse_data(data)

    # Should be interpolated (not carried forward)
    assert abs(result[1] - 9.2) < 0.001, f"Expected 9.2, got {result[1]}"
    assert abs(result[3] - 7.6) < 0.001, f"Expected 7.6, got {result[3]}"
    print("PASSED")


def test_mixed_gap_sizes():
    """
    Data with varying gap sizes: some minutes adjacent, some with 3-min gaps, some with 10-min gaps.
    """
    print("\n=== Test: Mixed gap sizes ===")
    data = {
        0: 10.0,
        1: 9.9,  # adjacent
        2: 9.8,  # adjacent
        5: 9.5,  # 3-min gap
        15: 8.5,  # 10-min gap
        16: 8.4,  # adjacent
    }
    result = interpolate_sparse_data(data)

    # Adjacent values preserved
    assert result[0] == 10.0
    assert result[1] == 9.9
    assert result[2] == 9.8

    # 3-min gap interpolated
    assert abs(result[3] - 9.7) < 0.001, f"Minute 3: expected ~9.7, got {result[3]}"
    assert abs(result[4] - 9.6) < 0.001, f"Minute 4: expected ~9.6, got {result[4]}"
    assert result[5] == 9.5

    # 10-min gap interpolated
    for m in range(6, 15):
        assert m in result, f"Missing minute {m}"
    assert abs(result[10] - 9.0) < 0.001, f"Minute 10: expected 9.0, got {result[10]}"

    # Adjacent after gap preserved
    assert result[15] == 8.5
    assert result[16] == 8.4

    # Total entries: 0-16 = 17
    assert len(result) == 17, f"Expected 17 entries, got {len(result)}"
    print("PASSED")


def test_full_day_sparse():
    """
    Full day simulation: 288 data points at 5-minute intervals (24 hours).
    Should produce 1441 dense entries (0 to 1440).
    """
    print("\n=== Test: Full day sparse data ===")
    sparse = {}
    total_minutes = 24 * 60  # 1440
    for m in range(0, total_minutes + 1, 5):
        # Simulate cumulative load decreasing backwards: ~15 kWh total consumption
        sparse[m] = 15.0 - (m / total_minutes) * 15.0

    result = interpolate_sparse_data(sparse)

    # Should have every minute from 0 to 1440
    assert len(result) == total_minutes + 1, f"Expected {total_minutes + 1} entries, got {len(result)}"

    # Energy preserved
    original_energy = sparse[0] - sparse[total_minutes]
    interpolated_energy = result[0] - result[total_minutes]
    assert abs(original_energy - interpolated_energy) < 0.0001, f"Energy not preserved: {original_energy} vs {interpolated_energy}"

    # Monotonically decreasing
    for m in range(1, total_minutes + 1):
        assert result[m] <= result[m - 1] + 0.0001, f"Not monotonic at minute {m}: {result[m]} > {result[m-1]}"

    print(f"  {len(sparse)} sparse points -> {len(result)} dense points")
    print(f"  Energy: {original_energy:.4f} kWh preserved")
    print("PASSED")


def test_increasing_values():
    """
    Interpolation should also work for increasing cumulative values
    (e.g. import data that increases going backwards).
    """
    print("\n=== Test: Increasing values ===")
    data = {0: 2.0, 5: 4.0, 10: 6.0}
    result = interpolate_sparse_data(data)

    assert len(result) == 11
    assert result[0] == 2.0
    assert abs(result[2] - 2.8) < 0.001
    assert result[5] == 4.0
    assert abs(result[7] - 4.8) < 0.001
    assert result[10] == 6.0
    print("PASSED")


def run_all_tests(my_predbat=None):
    """Run all interpolate_sparse_data tests"""
    print("\n" + "=" * 60)
    print("Running interpolate_sparse_data tests")
    print("=" * 60)

    try:
        test_empty_data()
        test_single_point()
        test_two_points_adjacent()
        test_two_points_with_gap()
        test_already_dense()
        test_sparse_5min_intervals()
        test_energy_preservation()
        test_midnight_reset_not_interpolated()
        test_no_reset_for_small_drops()
        test_mixed_gap_sizes()
        test_full_day_sparse()
        test_increasing_values()

        print("\n" + "=" * 60)
        print("ALL interpolate_sparse_data TESTS PASSED")
        print("=" * 60)
        return 0
    except AssertionError as e:
        print("\n" + "=" * 60)
        print(f"TEST FAILED: {e}")
        print("=" * 60)
        return 1
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"ERROR: {e}")
        import traceback

        traceback.print_exc()
        print("=" * 60)
        return 1


if __name__ == "__main__":
    result = run_all_tests()
    sys.exit(result)
