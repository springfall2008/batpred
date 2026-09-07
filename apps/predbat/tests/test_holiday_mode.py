# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Holiday mode behaviour inside the weighted-bucket historical load forecast (days_previous_auto).

Every test here runs at a non-zero minutes_now: the historical holiday off-by-one is invisible at
minutes_now == 0 (the faulty condition tod <= minutes_now is then true only for the midnight slot),
which is why the existing suite never caught it.
"""

from datetime import datetime, timezone, timedelta

from utils import MinuteArray
from const import PREDICT_STEP
from config import CONFIG_ITEMS
from tests.test_load_forecast_history import step_energy_at

MINUTES_DAY = 24 * 60
TOLERANCE = 2e-4  # the cumulative forecast is stored to 4dp, so a 5-minute bucket carries ~5e-5 of rounding


def day_back(minute_ago, minutes_now):
    """Wall-clock day a minutes-ago index belongs to (0 = today, 1 = yesterday, ...)."""
    if minute_ago <= minutes_now:
        return 0
    return ((minute_ago - minutes_now - 1) // MINUTES_DAY) + 1


def build_wall_clock_load(day_rates, minutes_now, num_days, extra=MINUTES_DAY):
    """
    Build a backwards-indexed cumulative load MinuteArray from per-wall-clock-day constant rates.

    day_rates maps a day-back number (1 = yesterday) to a constant kWh-per-minute rate. Unlike the
    minutes-ago bucketing in test_load_forecast_history, days here are aligned to wall-clock
    midnight so a sample "d whole days ago at time-of-day tod" lands on exactly day d for any tod.
    """
    size = minutes_now + num_days * MINUTES_DAY + extra

    def inc(i):
        """Per-minute load increment at a minutes-ago index."""
        return day_rates.get(day_back(max(i, 1), minutes_now), 0.0)

    data = {}
    data[size - 1] = 0.0
    for i in range(size - 2, -1, -1):
        data[i] = data[i + 1] + inc(i)
    return MinuteArray(data, size)


def build_holiday_map(holiday_days, minutes_now, num_days, extra=MINUTES_DAY):
    """Per-minute holiday_days_left history (minutes-ago -> value) for the given set of days-back."""
    size = minutes_now + num_days * MINUTES_DAY + extra
    return {i: (3.0 if day_back(i, minutes_now) in holiday_days else 0.0) for i in range(size)}


def age_factor(day):
    """Age weighting used by the forecast: 0.9 for yesterday, -0.03 per day, floor 0.1."""
    return max(0.1, 0.9 - (day - 1) * 0.03)


def weekday_factor(now_utc, day):
    """Weekday weighting used by the forecast for a day that is not holiday-matched."""
    today_dow = now_utc.weekday()
    hist_dow = (now_utc - timedelta(days=day)).weekday()
    if hist_dow == today_dow:
        return 1.0
    if (hist_dow >= 5) == (today_dow >= 5):
        return 0.7
    return 0.5


def weighted_mean(now_utc, day_rates, days, neutral_weekday=False):
    """Weighted mean of the 5-minute energy of the given days-back, mirroring the forecast weighting."""
    num = 0.0
    den = 0.0
    for d in days:
        weight = age_factor(d) if neutral_weekday else weekday_factor(now_utc, d) * age_factor(d)
        num += PREDICT_STEP * day_rates[d] * weight
        den += weight
    return num / den if den else 0.0


# Everything this module writes onto the shared PredBat fixture, including the two mocked methods. unit_test.py
# runs most modules against a single instance, so any of these left behind makes later modules order-dependent.
# Snapshot and restore is done through __dict__ rather than getattr/setattr: get_holiday_minutes and
# get_history_wrapper are class methods with no instance attribute, and assigning the bound method back would
# leave the instance permanently shadowing the class.
MUTATED_ATTRS = [
    "get_holiday_minutes",
    "get_history_wrapper",
    "now_utc",
    "minutes_now",
    "forecast_minutes",
    "plan_interval_minutes",
    "holiday_days_left",
    "holiday_load_scaling",
    "base_load",
    "car_charging_hold",
    "car_charging_energy",
    "iboost_energy_subtract",
    "iboost_energy_today",
    "max_days_previous",
    "load_minutes",
    "load_minutes_age",
]

_MISSING = object()


def snapshot_attrs(my_predbat):
    """Capture the instance-dict entry for every attribute this module overwrites."""
    return {name: my_predbat.__dict__.get(name, _MISSING) for name in MUTATED_ATTRS}


def restore_attrs(my_predbat, saved):
    """Put back what snapshot_attrs() captured, removing entries that were not instance attributes before."""
    for name, value in saved.items():
        if value is _MISSING:
            my_predbat.__dict__.pop(name, None)
        else:
            my_predbat.__dict__[name] = value


def setup_holiday(my_predbat, now_utc, minutes_now, day_rates, num_days, holiday_days_left):
    """Configure my_predbat for a deterministic holiday forecast computation at a non-zero minutes_now."""
    my_predbat.now_utc = now_utc
    my_predbat.minutes_now = minutes_now
    my_predbat.forecast_minutes = 2 * MINUTES_DAY
    my_predbat.plan_interval_minutes = 30
    my_predbat.holiday_days_left = holiday_days_left
    my_predbat.holiday_load_scaling = 0.7
    my_predbat.base_load = 0.0
    my_predbat.car_charging_hold = False
    my_predbat.car_charging_energy = None
    my_predbat.iboost_energy_subtract = False
    my_predbat.iboost_energy_today = None
    my_predbat.max_days_previous = 31
    my_predbat.load_minutes = build_wall_clock_load(day_rates, minutes_now, num_days)
    my_predbat.load_minutes_age = num_days


def test_holiday_mode(my_predbat):
    """
    Test holiday mode in the weighted-bucket load forecast: history window sizing, genuine exclusion
    of mismatched days, the holiday_load_scaling fallback and per-forward-day holiday state.
    """
    print("**** Running holiday_mode tests ****")
    failed = False

    now_utc = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)  # Wednesday noon
    minutes_now = 720
    original_get_holiday_minutes = my_predbat.get_holiday_minutes
    original_history = my_predbat.get_history_wrapper
    original_args = dict(my_predbat.args)
    original_attrs = snapshot_attrs(my_predbat)

    # ---------------------------------------------------------------
    # Test 1: the oldest day in the window is holiday-matched for every slot (off-by-one)
    # ---------------------------------------------------------------
    print("Test 1: oldest day holiday-matched either side of minutes_now")
    day_rates = {1: 0.01, 2: 0.02, 3: 0.05}  # day 3 is the pre-holiday day, 5x the load
    setup_holiday(my_predbat, now_utc, minutes_now, day_rates, 3, holiday_days_left=5)
    my_predbat.get_holiday_minutes = lambda now, n: build_holiday_map({1, 2}, minutes_now, 4)

    forecast = my_predbat.compute_load_forecast_history(now_utc)
    before_now = step_energy_at(forecast, 715)  # tod <= minutes_now: the slot the off-by-one corrupts
    after_now = step_energy_at(forecast, 725)  # tod > minutes_now: the slot it does not
    expected = weighted_mean(now_utc, day_rates, [1, 2], neutral_weekday=True)

    if abs(before_now - after_now) > TOLERANCE:
        print("ERROR: slots either side of minutes_now differ: {:.6f} vs {:.6f} (oldest day escaped the holiday match)".format(before_now, after_now))
        failed = True
    elif abs(before_now - expected) > TOLERANCE:
        print("ERROR: expected holiday-only mean {:.6f} got {:.6f}".format(expected, before_now))
        failed = True
    else:
        print("Oldest day excluded either side of minutes_now ({:.6f} both sides)".format(before_now))

    # ---------------------------------------------------------------
    # Test 2: get_holiday_minutes covers one more day than the forecast window
    # ---------------------------------------------------------------
    print("Test 2: holiday history window is num_days + 1")
    my_predbat.get_holiday_minutes = original_get_holiday_minutes
    requested = {}

    def capture_history(entity_id, days=30, required=True, tracked=True):
        """Record the days requested and return a single holiday-on record."""
        requested["days"] = days
        return [[{"state": "3", "last_updated": (now_utc - timedelta(days=20)).isoformat()}]]

    my_predbat.get_history_wrapper = capture_history
    holiday_minutes = my_predbat.get_holiday_minutes(now_utc, 7)
    if requested.get("days") != 8:
        print("ERROR: get_holiday_minutes requested {} days of history, expected 8 (num_days + 1)".format(requested.get("days")))
        failed = True
    elif holiday_minutes is None or holiday_minutes.get(8 * MINUTES_DAY - 1, 0) <= 0:
        print("ERROR: holiday map does not extend to the extra day (index {} missing)".format(8 * MINUTES_DAY - 1))
        failed = True
    else:
        print("get_holiday_minutes covers num_days + 1 days of history")
    my_predbat.get_history_wrapper = original_history
    my_predbat.get_holiday_minutes = original_get_holiday_minutes

    # ---------------------------------------------------------------
    # Test 3: first day of a holiday - no matching history, so holiday_load_scaling applies
    # ---------------------------------------------------------------
    print("Test 3: holiday_load_scaling on the first day")
    day_rates = {1: 0.01, 2: 0.02, 3: 0.03}
    setup_holiday(my_predbat, now_utc, minutes_now, day_rates, 3, holiday_days_left=5)
    my_predbat.get_holiday_minutes = lambda now, n: build_holiday_map(set(), minutes_now, 4)

    forecast = my_predbat.compute_load_forecast_history(now_utc)
    scaled = step_energy_at(forecast, 715)
    unscaled_mean = weighted_mean(now_utc, day_rates, [1, 2, 3])
    expected = unscaled_mean * 0.7

    if abs(scaled - expected) > TOLERANCE:
        print("ERROR: day-1 holiday expected {:.6f} ({:.6f} x 0.7) got {:.6f}".format(expected, unscaled_mean, scaled))
        failed = True
    else:
        print("First holiday day scaled by holiday_load_scaling ({:.6f} from {:.6f})".format(scaled, unscaled_mean))

    # The degenerate case the report calls out: without the scaling this is bit-identical to no holiday
    my_predbat.holiday_days_left = 0
    no_holiday = step_energy_at(my_predbat.compute_load_forecast_history(now_utc), 715)
    if abs(no_holiday - scaled) < TOLERANCE:
        print("ERROR: holiday mode had no effect at all on the first day (forecast identical to non-holiday)")
        failed = True
    else:
        print("First holiday day differs from the non-holiday forecast ({:.6f} vs {:.6f})".format(scaled, no_holiday))

    # ---------------------------------------------------------------
    # Test 4: a single day of holiday history retires the scaling for that slot
    # ---------------------------------------------------------------
    print("Test 4: one matching day replaces the scaling")
    setup_holiday(my_predbat, now_utc, minutes_now, day_rates, 3, holiday_days_left=5)
    my_predbat.get_holiday_minutes = lambda now, n: build_holiday_map({1}, minutes_now, 4)

    forecast = my_predbat.compute_load_forecast_history(now_utc)
    matched = step_energy_at(forecast, 715)
    expected = weighted_mean(now_utc, day_rates, [1], neutral_weekday=True)
    if abs(matched - expected) > TOLERANCE:
        print("ERROR: single matching day expected {:.6f} got {:.6f}".format(expected, matched))
        failed = True
    else:
        print("Single matching holiday day used exclusively ({:.6f})".format(matched))

    # ---------------------------------------------------------------
    # Test 5: no recorded holiday history at all still applies the scaling
    # ---------------------------------------------------------------
    print("Test 5: missing holiday history still acts")
    setup_holiday(my_predbat, now_utc, minutes_now, day_rates, 3, holiday_days_left=5)
    my_predbat.get_holiday_minutes = lambda now, n: None

    forecast = my_predbat.compute_load_forecast_history(now_utc)
    scaled = step_energy_at(forecast, 715)
    expected = weighted_mean(now_utc, day_rates, [1, 2, 3]) * 0.7
    if abs(scaled - expected) > TOLERANCE:
        print("ERROR: missing holiday history expected {:.6f} got {:.6f}".format(expected, scaled))
        failed = True
    else:
        print("Missing holiday history still applies holiday_load_scaling ({:.6f})".format(scaled))

    # ---------------------------------------------------------------
    # Test 6: returning home - holiday days are excluded, not merely down-weighted
    # ---------------------------------------------------------------
    print("Test 6: return day excludes holiday history")
    day_rates = {1: 0.01, 2: 0.01, 3: 0.04}  # days 1-2 were the holiday, day 3 normal
    setup_holiday(my_predbat, now_utc, minutes_now, day_rates, 3, holiday_days_left=0)
    my_predbat.get_holiday_minutes = lambda now, n: build_holiday_map({1, 2}, minutes_now, 4)

    forecast = my_predbat.compute_load_forecast_history(now_utc)
    returned = step_energy_at(forecast, 715)
    expected = weighted_mean(now_utc, day_rates, [3])
    if abs(returned - expected) > TOLERANCE:
        print("ERROR: return day expected non-holiday-only mean {:.6f} got {:.6f}".format(expected, returned))
        failed = True
    else:
        print("Return day uses non-holiday days only ({:.6f})".format(returned))

    # ---------------------------------------------------------------
    # Test 7: returning from a holiday longer than the window - inverse scaling, never zero
    # ---------------------------------------------------------------
    print("Test 7: return from a long holiday")
    setup_holiday(my_predbat, now_utc, minutes_now, day_rates, 3, holiday_days_left=0)
    my_predbat.get_holiday_minutes = lambda now, n: build_holiday_map({1, 2, 3}, minutes_now, 4)

    forecast = my_predbat.compute_load_forecast_history(now_utc)
    returned = step_energy_at(forecast, 715)
    expected = weighted_mean(now_utc, day_rates, [1, 2, 3]) / 0.7
    if returned <= 0:
        print("ERROR: return from a long holiday collapsed to zero load")
        failed = True
    elif abs(returned - expected) > TOLERANCE:
        print("ERROR: return from a long holiday expected {:.6f} got {:.6f}".format(expected, returned))
        failed = True
    else:
        print("Return from a long holiday scaled back up ({:.6f})".format(returned))

    # ---------------------------------------------------------------
    # Test 8: the forward day, not today, decides the holiday state of a slot
    # ---------------------------------------------------------------
    print("Test 8: per-forward-day holiday state")
    day_rates = {1: 0.01, 2: 0.01, 3: 0.04}
    setup_holiday(my_predbat, now_utc, minutes_now, day_rates, 3, holiday_days_left=1)  # today is the last holiday day
    my_predbat.get_holiday_minutes = lambda now, n: build_holiday_map({1, 2}, minutes_now, 4)

    forecast = my_predbat.compute_load_forecast_history(now_utc)
    today_slot = step_energy_at(forecast, 715)  # still on holiday
    tomorrow_slot = step_energy_at(forecast, MINUTES_DAY + 715)  # home again
    expected_today = weighted_mean(now_utc, day_rates, [1, 2], neutral_weekday=True)
    expected_tomorrow = weighted_mean(now_utc, day_rates, [3])

    if abs(today_slot - expected_today) > TOLERANCE:
        print("ERROR: today slot expected holiday-only {:.6f} got {:.6f}".format(expected_today, today_slot))
        failed = True
    elif abs(tomorrow_slot - expected_tomorrow) > TOLERANCE:
        print("ERROR: tomorrow (return day) slot expected non-holiday {:.6f} got {:.6f}".format(expected_tomorrow, tomorrow_slot))
        failed = True
    else:
        print("Holiday state resolved per forward day (today {:.6f}, return day {:.6f})".format(today_slot, tomorrow_slot))

    # ---------------------------------------------------------------
    # Test 9: holiday mode off is completely unaffected by any of the above
    # ---------------------------------------------------------------
    print("Test 9: no holiday mode is unchanged")
    setup_holiday(my_predbat, now_utc, minutes_now, day_rates, 3, holiday_days_left=0)
    my_predbat.get_holiday_minutes = lambda now, n: None

    forecast = my_predbat.compute_load_forecast_history(now_utc)
    plain = step_energy_at(forecast, 715)
    expected = weighted_mean(now_utc, day_rates, [1, 2, 3])
    if abs(plain - expected) > TOLERANCE:
        print("ERROR: non-holiday forecast expected {:.6f} got {:.6f}".format(expected, plain))
        failed = True
    else:
        print("Non-holiday forecast keeps the plain weekday/age weighting ({:.6f})".format(plain))

    # ---------------------------------------------------------------
    # Test 10: holiday_load_scaling is a configuration item
    # ---------------------------------------------------------------
    print("Test 10: holiday_load_scaling config item")
    item = [config for config in CONFIG_ITEMS if config["name"] == "holiday_load_scaling"]
    if not item:
        print("ERROR: holiday_load_scaling is not in CONFIG_ITEMS")
        failed = True
    elif item[0].get("default") != 0.7:
        print("ERROR: holiday_load_scaling default {} != 0.7".format(item[0].get("default")))
        failed = True
    else:
        my_predbat.fetch_config_options()
        if abs(getattr(my_predbat, "holiday_load_scaling", 0) - 0.7) > 1e-9:
            print("ERROR: fetch_config_options did not read holiday_load_scaling, got {}".format(getattr(my_predbat, "holiday_load_scaling", None)))
            failed = True
        else:
            print("holiday_load_scaling config item present and read (default 0.7)")

    # ---------------------------------------------------------------
    # Restore mocks/state
    # ---------------------------------------------------------------
    my_predbat.args.clear()
    my_predbat.args.update(original_args)
    my_predbat.fetch_config_options()
    # After fetch_config_options, so the config-derived values it rewrites do not clobber the snapshot
    restore_attrs(my_predbat, original_attrs)

    return failed
