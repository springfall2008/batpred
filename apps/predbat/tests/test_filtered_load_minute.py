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


def build_cumulative(increments, size):
    """Build a backwards cumulative MinuteArray so get_from_incrementing(data, i) == increments[i]."""
    data = {}
    data[size - 1] = 0.0
    for i in range(size - 2, -1, -1):
        data[i] = data[i + 1] + increments.get(i, 0.0)
    return MinuteArray(data, size)


def reset(my_predbat):
    """Reset the load-filtering related attributes to a clean (no filtering) baseline."""
    my_predbat.car_charging_hold = False
    my_predbat.car_charging_energy = None
    my_predbat.iboost_energy_subtract = False
    my_predbat.iboost_energy_today = None
    my_predbat.car_charging_threshold = 0.1
    my_predbat.car_charging_rate = [6.0]
    my_predbat.base_load = 0.0


def check(name, got, expected, tol=1e-9):
    """Compare a value to its expectation, printing and returning failure state."""
    if abs(got - expected) > tol:
        print("ERROR: {} expected {:.5f} got {:.5f}".format(name, expected, got))
        return True
    print("OK: {} = {:.5f}".format(name, got))
    return False


def test_filtered_load_minute(my_predbat):
    """
    Test get_filtered_load_window and get_filtered_load_minute (raw, car/iBoost filtering, averaging, base load).
    """
    print("**** Running filtered_load_minute tests ****")
    failed = False

    # ---------------------------------------------------------------
    # get_filtered_load_window
    # ---------------------------------------------------------------
    reset(my_predbat)
    data = build_cumulative({10: 0.1, 11: 0.2, 12: 0.3}, 40)

    # Raw, no filtering -> load == raw == sum of the window
    load, raw = my_predbat.get_filtered_load_window(data, [10, 11, 12], 3)
    failed |= check("window raw load", load, 0.6)
    failed |= check("window raw value", raw, 0.6)

    # Car charging energy subtraction (per measured car energy)
    reset(my_predbat)
    my_predbat.car_charging_hold = True
    my_predbat.car_charging_energy = build_cumulative({10: 0.05, 11: 0.05}, 40)
    load, raw = my_predbat.get_filtered_load_window(data, [10, 11, 12], 3)
    failed |= check("window minus car energy", load, 0.5)
    failed |= check("window raw unchanged", raw, 0.6)

    # iBoost subtraction
    reset(my_predbat)
    my_predbat.iboost_energy_subtract = True
    my_predbat.iboost_energy_today = build_cumulative({12: 0.2}, 40)
    load, _ = my_predbat.get_filtered_load_window(data, [10, 11, 12], 3)
    failed |= check("window minus iboost", load, 0.4)

    # Car charging hold threshold (no measured car energy): subtract the car rate when above the threshold
    reset(my_predbat)
    my_predbat.car_charging_hold = True
    my_predbat.car_charging_threshold = 0.1  # per minute -> window threshold 0.1 * step = 0.3
    my_predbat.car_charging_rate = [6.0]  # 6 kW -> 6 * 3 / 60 = 0.3 kWh removed
    load, _ = my_predbat.get_filtered_load_window(data, [10, 11, 12], 3)  # raw 0.6 >= 0.3
    failed |= check("window car hold threshold", load, 0.3)

    # Below threshold -> untouched
    small = build_cumulative({10: 0.05, 11: 0.05}, 40)
    load, _ = my_predbat.get_filtered_load_window(small, [10, 11, 12], 3)  # raw 0.1 < 0.3
    failed |= check("window below threshold kept", load, 0.1)

    # ---------------------------------------------------------------
    # get_filtered_load_minute - non-historical (single window)
    # ---------------------------------------------------------------
    reset(my_predbat)
    data = build_cumulative({5: 0.2, 6: 0.2}, 40)
    load, _ = my_predbat.get_filtered_load_minute(data, 5, historical=False, step=2)
    failed |= check("non-historical single window", load, 0.4)

    # ---------------------------------------------------------------
    # get_filtered_load_minute - historical weighted average across days
    # ---------------------------------------------------------------
    reset(my_predbat)
    my_predbat.days_previous = [1, 2]
    my_predbat.days_previous_weight = [1.0, 3.0]
    my_predbat.load_minutes_age = 2
    # At minute 0, step 2: day1 indices [1440, 1439], day2 indices [2880, 2879]
    hist = build_cumulative({1440: 0.2, 1439: 0.2, 2880: 0.4, 2879: 0.4}, 2 * 1440 + 10)
    # day1 window = 0.4, day2 window = 0.8 -> weighted (0.4*1 + 0.8*3) / 4 = 0.7
    load, _ = my_predbat.get_filtered_load_minute(hist, 0, historical=True, step=2)
    failed |= check("historical weighted average", load, 0.7)

    # ---------------------------------------------------------------
    # Base load floor and base_in_raw flag
    # ---------------------------------------------------------------
    reset(my_predbat)
    my_predbat.base_load = 0.1  # kW -> floor 0.1 * step / 60
    floor = 0.1 * 2 / 60.0
    low = build_cumulative({5: 0.0005, 6: 0.0005}, 40)  # window 0.001 < floor
    load, raw = my_predbat.get_filtered_load_minute(low, 5, historical=False, step=2, base_in_raw=True)
    failed |= check("base load floor", load, floor)
    failed |= check("base in raw on", raw, floor)
    load, raw = my_predbat.get_filtered_load_minute(low, 5, historical=False, step=2, base_in_raw=False)
    failed |= check("base load floor (raw kept)", load, floor)
    failed |= check("base in raw off keeps measured", raw, 0.001)

    # Restore the shared predbat config so this test does not leak load-filtering state into later tests
    my_predbat.fetch_config_options()

    return failed
