# fmt: off
# pylint: disable=line-too-long
"""
Tests for the `utc: true` option on basic_rates entries.

Manual rate windows are normally given in local wall-clock time. A meter whose schedule is fixed to
UTC - an Economy 7 smart meter, for instance - needs its windows entered in UTC instead, so that
they track the clocks changing rather than drifting an hour through British Summer Time.
"""

from datetime import datetime

import pytz


LONDON = pytz.timezone("Europe/London")


def _pin_clock(my_predbat, when):
    """Pin the predbat clock to local midnight on the given date, returning the UTC offset in minutes."""
    local_midnight = LONDON.localize(datetime(when.year, when.month, when.day, 0, 0, 0))
    my_predbat.midnight_utc = local_midnight
    my_predbat.midnight = datetime(when.year, when.month, when.day, 0, 0, 0)
    return int(local_midnight.utcoffset().total_seconds() // 60)


def _window_of(rates, rate_value):
    """Return the (first, last) minute of the day carrying the given rate."""
    minutes = [minute for minute in range(24 * 60) if abs(rates.get(minute, 0) - rate_value) < 0.0001]
    if not minutes:
        return None
    return min(minutes), max(minutes)


def test_basic_rates_utc(my_predbat):
    """
    Test the utc flag on basic_rates windows, restoring the shared clock afterwards.

    The whole suite runs against one PredBat instance, so the pinned clock these tests need must be
    put back or every later test inherits a date five days in the past and becomes order-dependent.
    """
    original_midnight_utc = my_predbat.midnight_utc
    original_midnight = my_predbat.midnight
    try:
        return _run_basic_rates_utc_tests(my_predbat)
    finally:
        my_predbat.midnight_utc = original_midnight_utc
        my_predbat.midnight = original_midnight


def _run_basic_rates_utc_tests(my_predbat):
    """
    Run the utc flag test cases against a clock the caller is responsible for restoring.

    Tests:
    - Test 1: utc:true during BST shifts a 00:30-07:30 window to 01:30-08:30 local
    - Test 2: utc:true during GMT leaves the window at 00:30-07:30 local
    - Test 3: without the flag the window stays at local wall-clock time in BST
    - Test 4: a window crossing midnight in UTC is shifted correctly
    """
    print("\n**** Running basic_rates utc option tests ****")
    failed = False

    # ------------------------------------------------------------------
    # Test 1: utc:true in BST -> 01:30-08:30 local
    # ------------------------------------------------------------------
    print("\n*** Test 1: utc:true during BST ***")
    offset = _pin_clock(my_predbat, datetime(2026, 8, 20))
    rates = my_predbat.basic_rates([{"start": "00:30:00", "end": "07:30:00", "rate": 13.0, "utc": True}], "rates_import", include_manual_api=False)
    window = _window_of(rates, 13.0)

    if offset != 60:
        print("ERROR: expected a +60 minute BST offset, got {}".format(offset))
        failed = True
    elif window != (90, 509):
        print("ERROR: BST utc window should cover 01:30-08:30 (minutes 90-509), got {}".format(window))
        failed = True
    else:
        print("PASS: utc:true window lands at 01:30-08:30 local during BST")

    # ------------------------------------------------------------------
    # Test 2: utc:true in GMT -> unchanged
    # ------------------------------------------------------------------
    print("\n*** Test 2: utc:true during GMT ***")
    offset = _pin_clock(my_predbat, datetime(2026, 1, 20))
    rates = my_predbat.basic_rates([{"start": "00:30:00", "end": "07:30:00", "rate": 13.0, "utc": True}], "rates_import", include_manual_api=False)
    window = _window_of(rates, 13.0)

    if offset != 0:
        print("ERROR: expected a 0 minute GMT offset, got {}".format(offset))
        failed = True
    elif window != (30, 449):
        print("ERROR: GMT utc window should cover 00:30-07:30 (minutes 30-449), got {}".format(window))
        failed = True
    else:
        print("PASS: utc:true window stays at 00:30-07:30 local during GMT")

    # ------------------------------------------------------------------
    # Test 3: no flag -> local wall-clock, unshifted, even in BST
    # ------------------------------------------------------------------
    print("\n*** Test 3: no utc flag during BST ***")
    _pin_clock(my_predbat, datetime(2026, 8, 20))
    rates = my_predbat.basic_rates([{"start": "00:30:00", "end": "07:30:00", "rate": 13.0}], "rates_import", include_manual_api=False)
    window = _window_of(rates, 13.0)

    if window != (30, 449):
        print("ERROR: unflagged window should stay at 00:30-07:30 local, got {}".format(window))
        failed = True
    else:
        print("PASS: unflagged window keeps local wall-clock time")

    # ------------------------------------------------------------------
    # Test 4: a UTC window crossing midnight
    # ------------------------------------------------------------------
    print("\n*** Test 4: utc:true window crossing midnight in BST ***")
    _pin_clock(my_predbat, datetime(2026, 8, 20))
    rates = my_predbat.basic_rates([{"start": "23:30:00", "end": "05:30:00", "rate": 7.0, "utc": True}], "rates_import", include_manual_api=False)
    # 23:30 UTC is 00:30 local, so the window runs 00:30-06:30 local on the same day
    if abs(rates.get(30, 0) - 7.0) > 0.0001 or abs(rates.get(389, 0) - 7.0) > 0.0001:
        print("ERROR: cross-midnight utc window should cover 00:30-06:30 local, got minute 30={} minute 389={}".format(rates.get(30), rates.get(389)))
        failed = True
    elif abs(rates.get(29, 0)) > 0.0001 or abs(rates.get(390, 0)) > 0.0001:
        print("ERROR: cross-midnight utc window leaked outside 00:30-06:30, minute 29={} minute 390={}".format(rates.get(29), rates.get(390)))
        failed = True
    else:
        print("PASS: cross-midnight utc window shifted correctly")

    return failed
