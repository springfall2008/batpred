# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Tests for update_time()'s clock.

Predbat used to keep two clocks: now_utc/midnight_utc from the configured timezone: setting, and
now/midnight from datetime.now() - the host's own timezone. Everything derived from the second one
(minutes_now, and every timestamp rendered by time_abs_str) therefore agreed with the first only
while the container's timezone happened to match the setting. When it did not, minutes_now was
offset from the midnight_utc-keyed rate and forecast data it indexes into, and every rendered
timestamp was offset from the data it described.

These tests pin a configured timezone that deliberately disagrees with the host, which is what
makes the two clocks separable at all: on a machine where they agree, the old and new code are
indistinguishable.
"""

from datetime import datetime, timedelta

import pytz

from const import PREDICT_STEP
from utils import minutes_since_midnight, minutes_since_yesterday

# Far enough from any plausible host timezone that the two cannot silently agree.
FAR_TIMEZONE = "Pacific/Kiritimati"  # UTC+14, and never observes DST


def _pinned_clock(my_predbat, timezone_name, skew=0):
    """Run update_time() with the given timezone: setting, returning what it produced.

    The suite shares one PredBat instance whose clock create_predbat() pins, so everything
    update_time() writes is restored before returning.
    """
    saved = {name: getattr(my_predbat, name) for name in ("now_utc", "midnight_utc", "minutes_now", "minutes_to_midnight", "difference_minutes", "local_tz", "now_utc_real")}
    saved_timezone = my_predbat.args.get("timezone")
    saved_skew = my_predbat.args.get("clock_skew")
    try:
        my_predbat.args["timezone"] = timezone_name
        if skew:
            my_predbat.args["clock_skew"] = skew
        my_predbat.update_time(print=False)
        return {
            "now_utc": my_predbat.now_utc,
            "midnight_utc": my_predbat.midnight_utc,
            "minutes_now": my_predbat.minutes_now,
            "minutes_to_midnight": my_predbat.minutes_to_midnight,
            "time_now_str": my_predbat.time_now_str(),
            "time_abs_str": my_predbat.time_abs_str(my_predbat.minutes_now),
        }
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)
        if saved_timezone is None:
            my_predbat.args.pop("timezone", None)
        else:
            my_predbat.args["timezone"] = saved_timezone
        if saved_skew is None:
            my_predbat.args.pop("clock_skew", None)
        else:
            my_predbat.args["clock_skew"] = saved_skew


def test_clock_follows_configured_timezone(my_predbat):
    """
    Test minutes_now follows the configured timezone rather than the host's clock.

    This is the failure the split clock caused: minutes_now indexes rate and forecast arrays that
    are keyed in minutes from midnight_utc, so it has to be measured from midnight_utc. Taking it
    from datetime.now() instead meant that a container whose timezone differed from the timezone:
    setting read "now" at the wrong offset into its own rates.
    """
    print("\n*** Test: minutes_now is measured from the configured timezone's midnight ***")
    failed = False

    clock = _pinned_clock(my_predbat, FAR_TIMEZONE)

    expected = int((clock["now_utc"] - clock["midnight_utc"]).total_seconds() / 60 / PREDICT_STEP) * PREDICT_STEP
    if clock["minutes_now"] != expected:
        print(f"ERROR: minutes_now {clock['minutes_now']} is not the offset from midnight_utc ({expected})")
        failed = True

    # Independently of how update_time computes it: it must be that timezone's wall clock. The two
    # are read moments apart, so compare the distance around the day rather than the plain
    # difference - at 23:59 update_time floors to 23:55 while this read is already 00:00, which is
    # five minutes apart, not 1435.
    wall_clock = datetime.now(pytz.timezone(FAR_TIMEZONE))
    wall_minutes = (wall_clock.hour * 60 + wall_clock.minute) // PREDICT_STEP * PREDICT_STEP
    distance = abs(clock["minutes_now"] - wall_minutes)
    distance = min(distance, 24 * 60 - distance)
    if distance > PREDICT_STEP:
        print(f"ERROR: minutes_now {clock['minutes_now']} does not match {FAR_TIMEZONE} wall clock {wall_minutes} - it is following the host clock")
        failed = True

    if clock["minutes_now"] % PREDICT_STEP != 0:
        print(f"ERROR: minutes_now {clock['minutes_now']} is not on a PREDICT_STEP boundary")
        failed = True

    if clock["minutes_to_midnight"] != 24 * 60 - clock["minutes_now"]:
        print(f"ERROR: minutes_to_midnight {clock['minutes_to_midnight']} does not complete the day")
        failed = True

    if not failed:
        print("PASS: minutes_now follows the configured timezone")
    return failed


def test_clock_time_strings_follow_midnight_utc(my_predbat):
    """
    Test the rendered time strings are anchored to midnight_utc.

    time_abs_str() renders minute offsets that come from midnight_utc-keyed data - the plan table,
    the rate windows, the logs. Rendering them against a second, differently-offset midnight made
    every published timestamp disagree with the data it labelled.
    """
    print("\n*** Test: time_now_str/time_abs_str render against midnight_utc ***")
    failed = False

    clock = _pinned_clock(my_predbat, FAR_TIMEZONE)

    expected_now = (clock["midnight_utc"] + timedelta(minutes=clock["minutes_now"])).strftime("%H:%M:%S")
    if clock["time_now_str"] != expected_now:
        print(f"ERROR: time_now_str {clock['time_now_str']} is not midnight_utc + minutes_now ({expected_now})")
        failed = True

    expected_abs = (clock["midnight_utc"] + timedelta(minutes=clock["minutes_now"])).strftime("%m-%d %H:%M:%S")
    if clock["time_abs_str"] != expected_abs:
        print(f"ERROR: time_abs_str {clock['time_abs_str']} is not midnight_utc + minutes_now ({expected_abs})")
        failed = True

    # The rendered date is that timezone's date, which is the point: at UTC+14 it is routinely
    # tomorrow relative to a European host.
    if not clock["time_abs_str"].startswith(clock["now_utc"].strftime("%m-%d")):
        print(f"ERROR: time_abs_str {clock['time_abs_str']} is not on now_utc's date {clock['now_utc'].strftime('%m-%d')}")
        failed = True

    if not failed:
        print("PASS: the rendered time strings follow midnight_utc")
    return failed


def test_clock_skew_still_applies(my_predbat):
    """
    Test clock_skew still moves the whole clock, now that there is only one of them.

    clock_skew is applied to now_utc before midnight_utc and minutes_now are derived from it, so a
    skew has to move all three together rather than only the field it is added to.
    """
    print("\n*** Test: clock_skew moves the single clock ***")
    failed = False

    plain = _pinned_clock(my_predbat, FAR_TIMEZONE)
    skewed = _pinned_clock(my_predbat, FAR_TIMEZONE, skew=60)

    difference = skewed["minutes_now"] - plain["minutes_now"]
    if difference < 0:
        difference += 24 * 60  # the skew carried the clock past midnight
    if abs(difference - 60) > PREDICT_STEP:
        print(f"ERROR: a 60 minute skew moved minutes_now by {difference}")
        failed = True

    if not failed:
        print("PASS: clock_skew moves minutes_now with now_utc")
    return failed


def test_minutes_since_yesterday_accepts_an_aware_clock(my_predbat):
    """
    Test minutes_since_yesterday works on the timezone-aware clock and keeps its old answer.

    It used to be handed the naive clock and built its comparison point with datetime.combine(),
    which drops tzinfo - so it raised TypeError the moment update_time passed it now_utc. The
    aware answer has to match what the naive one gave for the same wall-clock time.
    """
    print("\n*** Test: minutes_since_yesterday takes the aware clock ***")
    failed = False

    naive = datetime(2026, 3, 29, 14, 35, 0)
    aware = pytz.timezone("Europe/London").localize(naive)

    if minutes_since_yesterday(aware) != minutes_since_yesterday(naive):
        print(f"ERROR: aware {minutes_since_yesterday(aware)} != naive {minutes_since_yesterday(naive)}")
        failed = True

    # 14:35 is 875 minutes after midnight, and the count runs from one microsecond before it.
    if minutes_since_yesterday(aware) != 875:
        print(f"ERROR: expected 875 minutes since 23:59 yesterday, got {minutes_since_yesterday(aware)}")
        failed = True

    if not failed:
        print("PASS: minutes_since_yesterday accepts the aware clock unchanged")
    return failed


def test_minutes_since_midnight_helper(my_predbat):
    """
    Test the minutes_since_midnight helper both clocks share.

    update_time() (predbat.py) and PredHeat.update_pred() (predheat.py) both need the same
    calculation, and predheat's tests stub update_pred out entirely - so a copy of the formula
    there could drift back to the host clock with the suite still green. They call this instead,
    and it is tested here.
    """
    print("\n*** Test: minutes_since_midnight ***")
    failed = False

    london = pytz.timezone("Europe/London")
    cases = [
        ("2026-06-15 14:37", 14 * 60 + 35, "a plain summer afternoon, floored to PREDICT_STEP"),
        ("2026-06-15 00:00", 0, "midnight itself"),
        ("2026-06-15 23:59", 23 * 60 + 55, "the last slot of the day"),
        ("2026-03-29 12:07", 12 * 60 + 5, "the spring-forward day - a wall-clock difference, not 11 hours"),
        ("2026-10-25 12:07", 12 * 60 + 5, "the autumn-back day - likewise not 13 hours"),
    ]
    for stamp, expected, description in cases:
        now_utc = london.localize(datetime.strptime(stamp, "%Y-%m-%d %H:%M"))
        midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        result = minutes_since_midnight(now_utc, midnight_utc)
        if result != expected:
            print(f"ERROR: {stamp} ({description}) gave {result}, expected {expected}")
            failed = True

    if not failed:
        print("PASS: minutes_since_midnight holds across DST changes and day boundaries")
    return failed


def run_clock_tests(my_predbat):
    """Run all update_time clock tests"""
    failed = False
    failed |= test_clock_follows_configured_timezone(my_predbat)
    failed |= test_clock_time_strings_follow_midnight_utc(my_predbat)
    failed |= test_clock_skew_still_applies(my_predbat)
    failed |= test_minutes_since_yesterday_accepts_an_aware_clock(my_predbat)
    failed |= test_minutes_since_midnight_helper(my_predbat)
    return failed
