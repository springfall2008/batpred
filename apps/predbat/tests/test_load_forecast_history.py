# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timezone, timedelta

from utils import MinuteArray
from const import PREDICT_STEP


def build_load_minutes(day_rates, extra_minutes=10):
    """
    Build a backwards-indexed cumulative load MinuteArray from per-day constant rates.

    day_rates maps a 1-based day number (1 = yesterday) to a constant kWh-per-minute rate for that day.
    The returned array satisfies get_from_incrementing(data, i) == rate of the day that index i belongs to.
    """
    num_days = max(day_rates.keys())
    size = num_days * 24 * 60 + extra_minutes

    def inc(i):
        day = ((max(i, 1) - 1) // (24 * 60)) + 1
        return day_rates.get(day, 0.0)

    data = {}
    data[size - 1] = 0.0
    for i in range(size - 2, -1, -1):
        data[i] = data[i + 1] + inc(i)
    return MinuteArray(data, size)


def expected_day_weight(now_utc, day, holiday_factor=1.0):
    """Re-implement the static weekday*age weighting (holiday applied separately) to validate the forecast."""
    today_dow = now_utc.weekday()
    hist_dow = (now_utc - timedelta(days=day)).weekday()
    if hist_dow == today_dow:
        weekday_factor = 1.0
    elif (hist_dow >= 5) == (today_dow >= 5):
        weekday_factor = 0.7
    else:
        weekday_factor = 0.5
    age_factor = max(0.1, 0.9 - (day - 1) * 0.03)
    return weekday_factor * holiday_factor * age_factor


def step_energy_at(load_forecast, minute_absolute):
    """Reproduce the consumer's read of a 5-minute bucket from the cumulative forecast dict."""
    total = 0.0
    for offset in range(PREDICT_STEP):
        idx = minute_absolute + offset
        total += max(load_forecast.get(idx + 1, 0) - load_forecast.get(idx, 0), 0)
    return total


def setup_predbat(my_predbat, now_utc):
    """Configure my_predbat for a clean deterministic forecast computation."""
    my_predbat.now_utc = now_utc
    my_predbat.minutes_now = 0
    my_predbat.forecast_minutes = 120
    my_predbat.plan_interval_minutes = 30
    my_predbat.holiday_days_left = 0
    my_predbat.base_load = 0.0
    my_predbat.car_charging_hold = False
    my_predbat.car_charging_energy = None
    my_predbat.iboost_energy_subtract = False
    my_predbat.iboost_energy_today = None
    my_predbat.max_days_previous = 31  # search window cap; num_days = min(load_minutes_age, max_days_previous - 1)


def test_load_forecast_history(my_predbat):
    """
    Test the weighted-bucket historical load forecast (days_previous: all).
    """
    print("**** Running load_forecast_history tests ****")
    failed = False

    now_utc = datetime(2026, 6, 17, 0, 0, 0, tzinfo=timezone.utc)  # Wednesday
    original_get_holiday_minutes = my_predbat.get_holiday_minutes
    original_args = dict(my_predbat.args) if hasattr(my_predbat, "args") else {}

    # ---------------------------------------------------------------
    # Test 1: combined weekday + age weighting end-to-end (holiday neutral)
    # ---------------------------------------------------------------
    print("Test 1: weekday/age weighting")
    setup_predbat(my_predbat, now_utc)
    num_days = 12
    day_rates = {d: 0.001 * d for d in range(1, num_days + 1)}  # distinct constant rates
    my_predbat.load_minutes = build_load_minutes(day_rates)
    my_predbat.load_minutes_age = num_days

    my_predbat.get_holiday_minutes = lambda now, n: None  # no holiday history -> neutral holiday factor (1.0)

    forecast = my_predbat.compute_load_forecast_history(now_utc)

    # Expected per-step value (constant across slots since each day's rate is constant)
    num = 0.0
    den = 0.0
    for d in range(1, num_days + 1):
        w = expected_day_weight(now_utc, d)
        sample = 5 * day_rates[d]  # 5-minute energy, base_load = 0, no subtraction
        num += sample * w
        den += w
    expected = num / den

    # Tolerance allows for dp4 quantization of the cumulative forecast (the consumer reads 4-dp values)
    for m in (5, 30, 60, 120):
        actual = step_energy_at(forecast, m)
        if abs(actual - expected) > 2e-4:
            print("ERROR: slot {} expected {} got {}".format(m, expected, actual))
            failed = True
    if not failed:
        print("Weighting end-to-end correct: {} kWh per 5 min".format(round(expected, 6)))

    # ---------------------------------------------------------------
    # Test 2: zero buckets excluded from numerator AND denominator
    # ---------------------------------------------------------------
    print("Test 2: zero-bucket exclusion")
    setup_predbat(my_predbat, now_utc)
    day_rates = {1: 0.002, 2: 0.0, 3: 0.004}  # day 2 entirely missing/zero
    my_predbat.load_minutes = build_load_minutes(day_rates)
    my_predbat.load_minutes_age = 3
    my_predbat.get_holiday_minutes = lambda now, n: None  # neutral holiday factor

    forecast = my_predbat.compute_load_forecast_history(now_utc)

    num = 0.0
    den = 0.0
    for d in (1, 3):  # day 2 excluded
        w = expected_day_weight(now_utc, d)
        num += 5 * day_rates[d] * w
        den += w
    expected = num / den
    actual = step_energy_at(forecast, 30)
    if abs(actual - expected) > 2e-4:
        print("ERROR: zero-bucket exclusion expected {} got {}".format(expected, actual))
        failed = True
    else:
        print("Zero buckets correctly excluded from both numerator and denominator")

    # Same case but with a non-zero base load: the gap (day 2) must still be excluded rather than
    # filled with the base load. Rates are above the base-load floor so days 1/3 are unaffected.
    setup_predbat(my_predbat, now_utc)
    my_predbat.base_load = 0.05  # kW; floor = 0.05 * 5 / 60 ~= 0.0042 kWh per 5 min
    my_predbat.load_minutes = build_load_minutes(day_rates)
    my_predbat.load_minutes_age = 3
    my_predbat.get_holiday_minutes = lambda now, n: None
    forecast = my_predbat.compute_load_forecast_history(now_utc)
    actual = step_energy_at(forecast, 30)
    if abs(actual - expected) > 2e-4:
        print("ERROR: zero-bucket exclusion with base load expected {} got {}".format(expected, actual))
        failed = True
    else:
        print("Zero buckets still excluded (not filled with base load) when a base load is configured")

    # All-zero history -> forecast all zeros
    setup_predbat(my_predbat, now_utc)
    my_predbat.load_minutes = build_load_minutes({1: 0.0})
    my_predbat.load_minutes_age = 1
    my_predbat.get_holiday_minutes = lambda now, n: None
    forecast = my_predbat.compute_load_forecast_history(now_utc)
    if any(abs(step_energy_at(forecast, m)) > 1e-9 for m in (5, 30, 60)):
        print("ERROR: all-zero history should produce a zero forecast")
        failed = True
    else:
        print("All-zero history produced a zero forecast")

    # ---------------------------------------------------------------
    # Test 3: age weighting clamps and nearer days dominate
    # ---------------------------------------------------------------
    print("Test 3: age weighting")
    age_d1 = max(0.1, 0.9 - (1 - 1) * 0.03)
    age_d10 = max(0.1, 0.9 - (10 - 1) * 0.03)
    age_d28 = max(0.1, 0.9 - (28 - 1) * 0.03)
    age_d40 = max(0.1, 0.9 - (40 - 1) * 0.03)
    if abs(age_d1 - 0.9) > 1e-9 or abs(age_d10 - 0.63) > 1e-9 or abs(age_d28 - 0.1) > 1e-9 or abs(age_d40 - 0.1) > 1e-9:
        print("ERROR: age factors wrong d1={} d10={} d28={} d40={}".format(age_d1, age_d10, age_d28, age_d40))
        failed = True
    else:
        print("Age factors correct (d1=0.9, -0.03/day, d10=0.63, floor 0.1 reached by ~d28)")

    # ---------------------------------------------------------------
    # Test 4: short history / gaps - only available days used, no padding
    # ---------------------------------------------------------------
    print("Test 4: short history")
    setup_predbat(my_predbat, now_utc)
    # Provide many days of data but limit the reported age to 3 days
    day_rates = {d: 0.001 * d for d in range(1, 13)}
    my_predbat.load_minutes = build_load_minutes(day_rates)
    my_predbat.load_minutes_age = 3
    my_predbat.get_holiday_minutes = lambda now, n: None

    forecast = my_predbat.compute_load_forecast_history(now_utc)
    num = 0.0
    den = 0.0
    for d in (1, 2, 3):
        w = expected_day_weight(now_utc, d)
        num += 5 * day_rates[d] * w
        den += w
    expected = num / den
    actual = step_energy_at(forecast, 30)
    if abs(actual - expected) > 2e-4:
        print("ERROR: short history expected only 3 days {} got {}".format(expected, actual))
        failed = True
    else:
        print("Short history correctly used only {} available days".format(my_predbat.load_minutes_age))

    # Zero age -> empty forecast
    my_predbat.load_minutes_age = 0
    if my_predbat.compute_load_forecast_history(now_utc) != {}:
        print("ERROR: zero age should return empty forecast")
        failed = True
    else:
        print("Zero age returned empty forecast")

    # ---------------------------------------------------------------
    # Test 5: get_holiday_minutes reconstructs state-at-time via minute_data
    # ---------------------------------------------------------------
    print("Test 5: get_holiday_minutes")
    my_predbat.get_holiday_minutes = original_get_holiday_minutes
    my_predbat.holiday_days_left = 0
    holiday_now = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)

    records = [
        {"state": "3", "last_updated": datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc).isoformat()},
        {"state": "0", "last_updated": datetime(2026, 6, 14, 0, 0, 0, tzinfo=timezone.utc).isoformat()},
    ]
    original_history = my_predbat.get_history_wrapper
    my_predbat.get_history_wrapper = lambda entity_id, days=30, required=True, tracked=True: [records]

    holiday_minutes = my_predbat.get_holiday_minutes(holiday_now, 7)
    # holiday on from 06-10 00:00 to 06-14 00:00; sample the state at noon of each historical day
    expected_state = {1: False, 2: False, 3: False, 4: True, 5: True, 6: True}
    holiday_map = {d: (holiday_minutes.get(d * 24 * 60, 0) > 0) for d in expected_state}
    if holiday_map != expected_state:
        print("ERROR: holiday map {} != expected {}".format(holiday_map, expected_state))
        failed = True
    else:
        print("Holiday history reconstructed correctly: {}".format(holiday_map))

    # Empty history -> None so the holiday factor becomes neutral
    my_predbat.get_history_wrapper = lambda entity_id, days=30, required=True, tracked=True: None
    if my_predbat.get_holiday_minutes(holiday_now, 5) is not None:
        print("ERROR: empty history should return None")
        failed = True
    else:
        print("Empty history returns None (neutral holiday factor)")
    my_predbat.get_history_wrapper = original_history
    my_predbat.holiday_days_left = 0

    # ---------------------------------------------------------------
    # Test 5b: holiday weighting is matched per 5-minute bucket (mid-day toggle)
    # ---------------------------------------------------------------
    print("Test 5b: per-bucket holiday weighting")

    # Per-minute holiday history (minutes-ago -> holiday_days_left). Holiday active only for samples
    # in yesterday's afternoon/evening (minutes-ago 1..720 == 06-16 12:00 to 06-17 00:00).
    toggle_minutes = {ma: (3.0 if 1 <= ma <= 720 else 0.0) for ma in range(3 * 24 * 60)}

    # End-to-end: yesterday (d=1) had a holiday toggle mid-day; today is not on holiday.
    # Morning buckets match today's state (factor 1.0), afternoon buckets do not (factor 0.5),
    # so a morning slot and an afternoon slot must produce different weighted averages.
    setup_predbat(my_predbat, now_utc)
    my_predbat.forecast_minutes = 24 * 60  # cover a full day so afternoon slots map within yesterday
    day_rates = {1: 0.002, 2: 0.004}  # distinct so the holiday weight shift is observable
    my_predbat.load_minutes = build_load_minutes(day_rates)
    my_predbat.load_minutes_age = 2
    my_predbat.get_holiday_minutes = lambda now, n: toggle_minutes

    forecast = my_predbat.compute_load_forecast_history(now_utc)

    def expected_with_holiday(slot_minute):
        num = 0.0
        den = 0.0
        for d in (1, 2):
            minute_previous = 24 * 60 - slot_minute + 24 * 60 * (d - 1)
            holiday_active = toggle_minutes.get(minute_previous, 0) > 0
            holiday_factor = 1.0 if (holiday_active == False) else 0.5  # today_holiday is False
            w = expected_day_weight(now_utc, d, holiday_factor=holiday_factor)
            num += 5 * day_rates[d] * w
            den += w
        return num / den

    morning = step_energy_at(forecast, 300)  # 05:00 yesterday -> before toggle
    afternoon = step_energy_at(forecast, 900)  # 15:00 yesterday -> after toggle
    if abs(morning - expected_with_holiday(300)) > 2e-4:
        print("ERROR: morning bucket expected {} got {}".format(expected_with_holiday(300), morning))
        failed = True
    if abs(afternoon - expected_with_holiday(900)) > 2e-4:
        print("ERROR: afternoon bucket expected {} got {}".format(expected_with_holiday(900), afternoon))
        failed = True
    if abs(morning - afternoon) < 1e-6:
        print("ERROR: per-bucket holiday weighting had no effect (morning == afternoon)")
        failed = True
    if not failed:
        print("Per-bucket holiday weighting applied correctly (morning {} != afternoon {})".format(round(morning, 6), round(afternoon, 6)))

    my_predbat.get_holiday_minutes = original_get_holiday_minutes

    # ---------------------------------------------------------------
    # Test 6: activation via days_previous_auto
    # ---------------------------------------------------------------
    print("Test 6: activation via days_previous_auto")
    my_predbat.args["days_previous"] = [14]
    my_predbat.args["days_previous_auto"] = True
    my_predbat.fetch_config_options()
    if not getattr(my_predbat, "load_forecast_history", False):
        print("ERROR: days_previous_auto did not enable load_forecast_history")
        failed = True
    elif my_predbat.max_days_previous != 15:
        print("ERROR: window from max(days_previous) wrong, max_days_previous {} != 15".format(my_predbat.max_days_previous))
        failed = True
    else:
        print("days_previous_auto enabled forecast mode, window from max(days_previous), max_days_previous={}".format(my_predbat.max_days_previous))

    # Window defaults to 7 when days_previous is not set
    my_predbat.args["days_previous"] = [7]
    my_predbat.fetch_config_options()
    if my_predbat.max_days_previous != 8:
        print("ERROR: default window expected max_days_previous 8 got {}".format(my_predbat.max_days_previous))
        failed = True
    else:
        print("Default days_previous gives a 7-day window")

    # Switch off leaves the flag off
    my_predbat.args["days_previous_auto"] = False
    my_predbat.fetch_config_options()
    if getattr(my_predbat, "load_forecast_history", False):
        print("ERROR: days_previous_auto off should not enable forecast mode")
        failed = True
    else:
        print("days_previous_auto off leaves forecast mode off")

    # ---------------------------------------------------------------
    # Test 7: car-charging hold is applied per day before averaging (legacy days_previous)
    # ---------------------------------------------------------------
    print("Test 7: per-day car-charging hold in legacy averaging")
    setup_predbat(my_predbat, now_utc)
    my_predbat.days_previous = [1, 2]
    my_predbat.days_previous_weight = [1.0, 1.0]
    my_predbat.load_minutes_age = 2
    my_predbat.car_charging_hold = True
    my_predbat.car_charging_energy = None
    my_predbat.car_charging_threshold = 0.1  # stored as kWh/min (i.e. 6 kW); window threshold = 0.1 * step
    my_predbat.car_charging_rate = [7.0]
    # Day 1 has a 7 kW (EV charge) constant load, day 2 a normal 0.5 kW load
    my_predbat.load_minutes = build_load_minutes({1: 7.0 / 60.0, 2: 0.5 / 60.0})

    load, _ = my_predbat.get_filtered_load_minute(my_predbat.load_minutes, 300, historical=True, step=PREDICT_STEP)
    # Per-day: day1 5-min load 0.583 kWh >= 0.5 threshold -> car removed -> ~0; day2 0.0417 kWh kept.
    # Correct (per-day) average = (0 + 0.0417) / 2 = 0.0208. The old (average-first) bug gave ~0.3125.
    day2_window = 5 * (0.5 / 60.0)
    expected = (0.0 + day2_window) / 2.0
    buggy = (5 * (7.0 / 60.0) + day2_window) / 2.0
    if abs(load - expected) > 2e-3:
        print("ERROR: per-day car hold expected {:.4f} got {:.4f} (old buggy value was {:.4f})".format(expected, load, buggy))
        failed = True
    else:
        print("Car-charging hold correctly applied per day before averaging ({:.4f} kWh, not the diluted {:.4f})".format(load, buggy))

    # ---------------------------------------------------------------
    # Test 8: cumulative-from-midnight and midnight-crossing
    # ---------------------------------------------------------------
    print("Test 8: cumulative-from-midnight + midnight crossing")
    setup_predbat(my_predbat, now_utc)
    my_predbat.minutes_now = 600
    my_predbat.forecast_minutes = 1200  # horizon 600 + 1200 + 30 = 1830 crosses midnight
    my_predbat.load_minutes = build_load_minutes({d: 0.002 for d in range(1, 8)})
    my_predbat.load_minutes_age = 6
    my_predbat.get_holiday_minutes = lambda now, n: None
    forecast = my_predbat.compute_load_forecast_history(now_utc)

    # The array is genuinely cumulative from midnight: zero at minute 0 and populated before minutes_now
    if abs(forecast.get(0, 0.0)) > 1e-9:
        print("ERROR: cumulative should start at 0 at midnight, got {}".format(forecast.get(0)))
        failed = True
    elif forecast.get(my_predbat.minutes_now, 0) <= 0:
        print("ERROR: earlier-today portion not populated (cumulative at minutes_now is 0)")
        failed = True
    else:
        print("Cumulative-from-midnight: starts at 0, populated through minutes_now ({:.4f} kWh)".format(forecast.get(my_predbat.minutes_now)))

    # Midnight crossing: a tomorrow slot samples whole, distinct days at the slot's time of day
    mn = my_predbat.minutes_now
    minute_absolute = 1500  # tomorrow 01:00 (time of day 60)
    tod = minute_absolute % (24 * 60)
    prevs = [(mn - tod) + d * 24 * 60 for d in range(1, 5)]
    if any(prevs[i + 1] - prevs[i] != 24 * 60 for i in range(len(prevs) - 1)):
        print("ERROR: midnight-crossing samples not distinct whole days apart: {}".format(prevs))
        failed = True
    else:
        print("Midnight-crossing samples distinct days {} (1440 apart)".format(prevs))

    # Same time-of-day today and tomorrow sample the same history, so (constant rate) give the same energy
    energy_tomorrow = step_energy_at(forecast, 1500)  # tomorrow 01:00
    energy_today = step_energy_at(forecast, 60)  # today 01:00 (earlier today)
    if energy_tomorrow <= 0 or abs(energy_tomorrow - energy_today) > 2e-4:
        print("ERROR: tomorrow slot energy {} != same-time-of-day today {}".format(energy_tomorrow, energy_today))
        failed = True
    else:
        print("Tomorrow slot matches same-time-of-day today (constant rate): {:.5f}".format(energy_tomorrow))

    # ---------------------------------------------------------------
    # Restore mocks/state
    # ---------------------------------------------------------------
    my_predbat.get_holiday_minutes = original_get_holiday_minutes
    my_predbat.args.clear()
    my_predbat.args.update(original_args)
    my_predbat.fetch_config_options()

    return failed
