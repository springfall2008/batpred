"""
Test fill_load_from_history - backfilling flat (missing) stretches of load_today from the
history of Predbat's own predbat.load_energy_h0 sensor
"""

import sys
import os

# Add the apps/predbat directory to the path
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import math
from datetime import datetime, timedelta, timezone
from fetch import Fetch
from utils import MinuteArray

NOW_UTC = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
MAX_DAYS_PREVIOUS = 4
SIZE = MAX_DAYS_PREVIOUS * 24 * 60 + 2


class TestFetch(Fetch):
    """Test class that inherits from Fetch to access its methods"""

    def __init__(self, history=None, args=None):
        """Minimal attributes fill_load_from_history uses, with a canned load_energy_h0 history."""
        self.log_messages = []
        self.prefix = "predbat"
        self.plan_interval_minutes = 30
        self.max_days_previous = MAX_DAYS_PREVIOUS
        self.now_utc = NOW_UTC
        self.history = history
        self.args = args or {}
        self.history_requests = []

    def log(self, message):
        """Capture log messages"""
        self.log_messages.append(message)
        print(message)

    def get_arg(self, name, default=None, **kwargs):
        """Return a test override or the default"""
        return self.args.get(name, default)

    def get_history_wrapper(self, entity_id, days=30, required=True, tracked=True):
        """Return the canned history and record the request"""
        self.history_requests.append((entity_id, days, required))
        return self.history


def h0_history(power_kw, days, step=5, source=None, switch_minute_ago=None, missing=None):
    """
    Build a predbat.load_energy_h0 history: the since-midnight load total, recorded every step minutes
    for days back from NOW_UTC, oldest first as Home Assistant returns it.

    power_kw(minutes_ago) gives the load at that time. switch_minute_ago simulates a load sensor change
    mid-day: from then on the total restarts from zero, as the new sensor only has history since the switch.
    source is the source attribute, or a function of minutes_ago giving it. Records whose minutes_ago falls in
    missing (start, end) are left out, as when Predbat was not running.
    """
    records = []
    start = NOW_UTC - timedelta(days=days)
    total = 0.0
    when = start
    last_day = start.date()
    while when <= NOW_UTC:
        minutes_ago = int((NOW_UTC - when).total_seconds() / 60)
        if when.date() != last_day:
            total = 0.0
            last_day = when.date()
        if switch_minute_ago is not None and minutes_ago == switch_minute_ago:
            total = 0.0
        attributes = {"unit_of_measurement": "kWh"}
        record_source = source(minutes_ago) if callable(source) else source
        if record_source:
            attributes["source"] = record_source
        if not (missing and missing[0] <= minutes_ago < missing[1]):
            records.append({"state": str(round(total, 3)), "last_updated": when.isoformat(), "attributes": attributes})
        # The next step minutes are backwards minute indices minutes_ago - 1 down to minutes_ago - step
        for minute in range(step):
            total += power_kw(minutes_ago - 1 - minute) / 60.0
        when += timedelta(minutes=step)
    return [records]


def load_today_minutes(power_kw, covered_minutes, flat=None):
    """
    Build load_today minute data as minute_data_load returns it: backwards cumulative kWh, minute 0 = now.

    Minutes older than covered_minutes are padded flat (the sensor did not exist yet), and each (start, end)
    in flat is a stretch where the sensor stopped counting.
    """
    flat = flat or []
    data = {}
    total = 1000.0
    for minute in range(SIZE):
        data[minute] = round(total, 4)
        stopped = minute >= covered_minutes or any(start <= minute < end for start, end in flat)
        if not stopped:
            total -= power_kw(minute) / 60.0
    return MinuteArray(data, SIZE)


def energy(data, newer, older):
    """Energy consumed between two backwards minute indices"""
    return data[newer] - data[older]


def test_no_gaps_does_not_fetch_history():
    """A load_today with no flat stretches is returned unchanged and load_energy_h0 history is never fetched"""
    fetch = TestFetch(history=h0_history(lambda m: 1.0, 4))
    load = load_today_minutes(lambda m: 1.0, SIZE)

    result, age = fetch.fill_load_from_history(load, 4)

    assert fetch.history_requests == [], "history should not be fetched when there are no gaps, got {}".format(fetch.history_requests)
    assert age == 4
    for minute in range(0, SIZE, 97):
        assert result[minute] == load[minute], "minute {} changed from {} to {}".format(minute, load[minute], result[minute])


def test_older_days_backfilled_after_sensor_change():
    """load_today only covers the last day (sensor changed); older days are rebuilt from load_energy_h0 and the age extended"""
    fetch = TestFetch(history=h0_history(lambda m: 2.0, 3, step=5))
    load = load_today_minutes(lambda m: 1.0, 24 * 60)

    result, age = fetch.fill_load_from_history(load, 1)

    assert fetch.history_requests and fetch.history_requests[0][0] == "predbat.load_energy_h0", fetch.history_requests
    # The day load_today covers keeps its own data
    assert abs(energy(result, 0, 24 * 60) - 24.0) < 0.01, "day 1 should stay at load_today's 24kWh, got {}".format(energy(result, 0, 24 * 60))
    # Days 2 and 3 come from load_energy_h0 at 2kW (48kWh/day), allowing for the midnight reset losing a sample
    for day in (2, 3):
        got = energy(result, (day - 1) * 24 * 60, day * 24 * 60)
        assert abs(got - 48.0) < 0.5, "day {} should be ~48kWh from load_energy_h0, got {}".format(day, got)
    assert age == 3, "age should extend to load_energy_h0's 3 days of history, got {}".format(age)


def test_flat_inside_history_not_filled():
    """
    A flat stretch inside load_today's own history is not filled, however much load_energy_h0 rose over it.
    load_energy_h0 was published from this same sensor there, so a rise is either a counter step a cycle late or
    an earlier fill read back - here 10kW the house never drew, as v9.4.1's runaway published (#5652)
    """
    fetch = TestFetch(history=h0_history(lambda m: 10.0 if 600 <= m < 900 else 1.0, 4))
    load = load_today_minutes(lambda m: 1.0, SIZE, flat=[(600, 900)])

    result, age = fetch.fill_load_from_history(load, 4)

    assert fetch.history_requests == [], "history should not be fetched for a flat inside load_today's history, got {}".format(fetch.history_requests)
    assert energy(result, 600, 900) == 0, "the flat stretch should stay flat for the later fillers, got {}".format(energy(result, 600, 900))
    assert age == 4


def test_gap_left_alone_when_h0_also_flat():
    """When load_energy_h0 recorded nothing before load_today started either, the days stay flat for the later fillers"""
    fetch = TestFetch(history=h0_history(lambda m: 0.0 if m >= 24 * 60 else 1.0, 4))
    load = load_today_minutes(lambda m: 1.0, 24 * 60)

    result, age = fetch.fill_load_from_history(load, 1)

    assert energy(result, 24 * 60, 3 * 24 * 60) == 0, "days before load_today should stay flat, got {}".format(energy(result, 24 * 60, 3 * 24 * 60))
    assert abs(energy(result, 0, 24 * 60) - 24.0) < 0.001
    assert age == 1, "age should not extend over days load_energy_h0 did not measure, got {}".format(age)


def test_sensor_switch_reset_adds_no_energy():
    """load_energy_h0 restarting from zero mid-day (the switch day) is a reset, not consumption, and not a negative step"""
    fetch = TestFetch(history=h0_history(lambda m: 1.0, 4, switch_minute_ago=750))
    load = load_today_minutes(lambda m: 1.0, 600)

    result, _age = fetch.fill_load_from_history(load, 0)

    # 900 minutes at 1kW, less at most the samples lost at the switch and at midnight
    gap = energy(result, 600, 1500)
    assert 14.8 <= gap <= 15.05, "stretch should hold ~15kWh at 1kW minus at most the reset samples, got {}".format(gap)
    for minute in range(0, 1500):
        assert result[minute] >= result[minute + 1], "data must keep incrementing, minute {} {} < {}".format(minute, result[minute], result[minute + 1])


def test_forecast_source_records_ignored():
    """Records published while Predbat ran on load_forecast alone are forecasts, not measurements, and are skipped"""
    fetch = TestFetch(history=h0_history(lambda m: 2.0, 4, source="load_forecast"))
    load = load_today_minutes(lambda m: 1.0, 24 * 60)

    result, _age = fetch.fill_load_from_history(load, 1)

    assert energy(result, 24 * 60, 3 * 24 * 60) == 0, "forecast records must not fill, got {}".format(energy(result, 24 * 60, 3 * 24 * 60))


def test_measured_source_records_used():
    """Records tagged as measured from load_today are used"""
    fetch = TestFetch(history=h0_history(lambda m: 2.0, 4, source="load_today"))
    load = load_today_minutes(lambda m: 1.0, 24 * 60)

    result, _age = fetch.fill_load_from_history(load, 1)

    # Less at most the 5 minute sample lost at the midnight reset
    got = energy(result, 24 * 60, 2 * 24 * 60)
    assert 47.8 <= got <= 48.05, "day 2 should hold ~48kWh, got {}".format(got)


def test_empty_load_today_built_from_h0():
    """A brand new load_today sensor with no history at all is built entirely from load_energy_h0"""
    fetch = TestFetch(history=h0_history(lambda m: 1.0, 2))

    result, age = fetch.fill_load_from_history({}, 0)

    assert len(result) == SIZE, "result should cover max_days_previous, got {}".format(len(result))
    assert abs(energy(result, 0, 24 * 60) - 24.0) < 0.5, "day 1 should be ~24kWh, got {}".format(energy(result, 0, 24 * 60))
    assert age == 2


def test_no_history_leaves_data_alone():
    """No load_energy_h0 history (fresh install, or no recorder) leaves the data and age as they were"""
    fetch = TestFetch(history=None)
    load = load_today_minutes(lambda m: 1.0, 24 * 60)

    result, age = fetch.fill_load_from_history(load, 1)

    assert age == 1
    assert energy(result, 24 * 60, 2 * 24 * 60) == 0


def test_disabled_by_config():
    """load_history_fill_enable: false turns the backfill off without fetching history"""
    fetch = TestFetch(history=h0_history(lambda m: 2.0, 3), args={"load_history_fill_enable": False})
    load = load_today_minutes(lambda m: 1.0, 24 * 60)

    result, age = fetch.fill_load_from_history(load, 1)

    assert fetch.history_requests == []
    assert age == 1
    assert energy(result, 24 * 60, 2 * 24 * 60) == 0


def test_short_flat_at_window_start_ignored():
    """
    A history that only lists changes can open hours into the window on a quiet night. Under a day of flat at
    the oldest end is not a missing sensor, and load_energy_h0 there came from this same sensor
    """
    fetch = TestFetch(history=h0_history(lambda m: 3.0, 4))
    load = load_today_minutes(lambda m: 1.0, SIZE - 600)

    result, age = fetch.fill_load_from_history(load, 3)

    assert fetch.history_requests == [], "10 hours of flat at the window start should not fetch history, got {}".format(fetch.history_requests)
    assert energy(result, SIZE - 600, SIZE - 1) == 0
    assert age == 3


def test_fetch_load_today_backfills_before_today_so_far():
    """
    fetch_load_today() backfills before working out today's load so far, so a sensor changed earlier today
    still counts the energy load_energy_h0 recorded before it started
    """
    fetch = TestFetch(history=h0_history(lambda m: 3.0 if m >= 300 else 1.0, 4))
    fetch.minutes_now = 12 * 60
    load = load_today_minutes(lambda m: 1.0, 300)
    fetch.minute_data_load = lambda now_utc, entity_name, max_days_previous, **kwargs: (load, 0)

    fetch.fetch_load_today()

    # 5 hours at 1kW from the new load_today plus the 7 hours before it at 3kW from load_energy_h0
    assert abs(fetch.load_minutes_now - 26.0) < 0.05, "today so far should be ~26kWh, got {}".format(fetch.load_minutes_now)
    assert fetch.load_minutes_age == 4, "age should extend to load_energy_h0's 4 days, got {}".format(fetch.load_minutes_age)
    assert abs(fetch.load_last_period - 1.0) < 0.01, "last period should be 1kW, got {}".format(fetch.load_last_period)


def test_load_energy_h0_published_unscaled_with_source(my_predbat):
    """
    load_energy_h0 is the backfill's source, so it must carry today's measured load as read - not scaled by
    load_scaling - and say where that came from, so forecast-only values can be told apart
    """
    print("**** test_load_energy_h0_published_unscaled_with_source ****")
    from tests.test_infra import reset_inverter, reset_rates
    from prediction import Prediction

    saved = {name: getattr(my_predbat, name, None) for name in ("load_minutes_now", "load_scaling", "load_minutes_source", "debug_enable")}
    try:
        reset_inverter(my_predbat)
        reset_rates(my_predbat, 10.0, 5.0)
        my_predbat.debug_enable = False
        my_predbat.load_minutes_now = 5.0
        my_predbat.load_scaling = 1.5
        my_predbat.load_minutes_source = "load_today"
        step = {minute: 0.0 for minute in range(0, my_predbat.forecast_minutes, 5)}
        my_predbat.prediction = Prediction(my_predbat, step, step, step, step)
        my_predbat.run_prediction([], [], [], [], False, save="base", end_record=my_predbat.end_record)

        published = my_predbat.dashboard_values[my_predbat.prefix + ".load_energy_h0"]
        assert published["state"] == 5.0, "load_energy_h0 should be today's unscaled load 5.0, got {}".format(published["state"])
        assert published["attributes"].get("source") == "load_today", "load_energy_h0 should say its source, got {}".format(published["attributes"])
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)


def test_forecast_interval_not_bridged():
    """
    Measured records either side of a stretch published from load_forecast must not be joined into one counter:
    what was consumed in between is unknown, so it is left for the later fillers rather than interpolated
    """
    fetch = TestFetch(history=h0_history(lambda m: 1.0, 4, source=lambda m: "load_forecast" if 200 <= m < 500 else "load_today"))
    load = load_today_minutes(lambda m: 1.0, 200)

    result, _age = fetch.fill_load_from_history(load, 0)

    assert energy(result, 200, 500) == 0, "the forecast-only stretch should stay unfilled, got {}".format(energy(result, 200, 500))
    assert abs(energy(result, 500, 700) - 200 / 60.0) < 0.05, "measured records before it should still fill, got {}".format(energy(result, 500, 700))


def test_missing_records_not_bridged():
    """A stretch longer than load_filter_threshold with no load_energy_h0 records (Predbat not running) is unknown, not interpolated"""
    fetch = TestFetch(history=h0_history(lambda m: 1.0, 4, missing=(205, 500)))
    load = load_today_minutes(lambda m: 1.0, 200)

    result, _age = fetch.fill_load_from_history(load, 0)

    assert energy(result, 200, 500) == 0, "the stretch with no records should stay unfilled, got {}".format(energy(result, 200, 500))


def test_short_record_gaps_still_bridged():
    """Records a few cycles apart (Home Assistant only records changes) are still joined up"""
    fetch = TestFetch(history=h0_history(lambda m: 1.0, 4, step=15))
    load = load_today_minutes(lambda m: 1.0, 200)

    result, _age = fetch.fill_load_from_history(load, 0)

    assert abs(energy(result, 200, 500) - 5.0) < 0.05, "gap should hold ~5kWh, got {}".format(energy(result, 200, 500))


def test_stalled_sensor_does_not_feed_back():
    """
    A new load_today sensor that has reported nothing is built from load_energy_h0, whose value is then
    republished each cycle. Today's load so far must hold still, not keep climbing at the last rate seen by
    extrapolating each cycle past the value it published the cycle before
    """
    history = h0_history(lambda m: 1.0, 2)
    fetch = TestFetch(history=history)
    fetch.minute_data_load = lambda now_utc, entity_name, max_days_previous, **kwargs: ({}, 0)
    seen = []
    for cycle in range(12):
        fetch.now_utc = NOW_UTC + timedelta(minutes=5 * cycle)
        fetch.minutes_now = 12 * 60 + 5 * cycle
        fetch.fetch_load_today()
        seen.append(fetch.load_minutes_now)
        # Publish today's load so far as load_energy_h0, as run_prediction() does
        history[0].append({"state": str(round(fetch.load_minutes_now, 3)), "last_updated": fetch.now_utc.isoformat(), "attributes": {"unit_of_measurement": "kWh", "source": "load_today"}})

    assert max(seen) - min(seen) < 0.01, "today's load should hold still while the sensor reports nothing, got {}".format(seen)


def test_coarse_counter_does_not_run_away():
    """
    A Solis-style load counter in whole kWh refreshed hourly sits flat past load_filter_threshold most of the
    time. Run fetch_load_today() every 15 minutes for a day, republishing load_energy_h0 every 5 minutes from
    the result with the state only, as a history backend that drops attributes keeps it. Today's load must
    track the counter, not climb by the fill's own output as v9.4.1's did, to 139kWh in a day (#5652)
    """
    power_kw = 0.3
    midnight = NOW_UTC.replace(hour=0)

    def counter(when):
        """The counter's whole kWh as of the last hourly refresh, since midnight"""
        refreshed = when.replace(minute=0, second=0)
        return math.floor(power_kw * (refreshed - midnight).total_seconds() / 3600)

    def load_today_at(now):
        """load_today minute data at now: the counter today, a steady 0.3kW before midnight"""
        data = {}
        for minute in range(SIZE):
            when = now - timedelta(minutes=minute)
            data[minute] = 1000.0 + counter(when) if when >= midnight else 1000.0 - power_kw * (midnight - when).total_seconds() / 3600
        return MinuteArray(data, SIZE)

    history = [[]]
    fetch = TestFetch(history=history)
    worst = 0.0
    for cycle in range(0, 24 * 12, 3):
        now = midnight + timedelta(minutes=5 * cycle)
        fetch.now_utc = now
        fetch.minutes_now = 5 * cycle
        load = load_today_at(now)
        fetch.minute_data_load = lambda now_utc, entity_name, max_days_previous, **kwargs: (load, 4)
        fetch.fetch_load_today()
        worst = max(worst, fetch.load_minutes_now - counter(now))
        for offset in range(3):
            published = now + timedelta(minutes=5 * offset)
            history[0].append({"state": str(round(fetch.load_minutes_now, 3)), "last_updated": published.isoformat(), "attributes": {}})

    assert worst < 0.01, "today's load should equal the counter, but ran up to {}kWh above it".format(round(worst, 2))


def run_all_tests(my_predbat=None):
    """Run all tests"""
    print("\n" + "=" * 60)
    print("Running fill_load_from_history tests")
    print("=" * 60)

    try:
        test_no_gaps_does_not_fetch_history()
        test_older_days_backfilled_after_sensor_change()
        test_flat_inside_history_not_filled()
        test_gap_left_alone_when_h0_also_flat()
        test_sensor_switch_reset_adds_no_energy()
        test_forecast_source_records_ignored()
        test_measured_source_records_used()
        test_empty_load_today_built_from_h0()
        test_no_history_leaves_data_alone()
        test_disabled_by_config()
        test_short_flat_at_window_start_ignored()
        test_fetch_load_today_backfills_before_today_so_far()
        test_forecast_interval_not_bridged()
        test_missing_records_not_bridged()
        test_short_record_gaps_still_bridged()
        test_stalled_sensor_does_not_feed_back()
        test_coarse_counter_does_not_run_away()
        if my_predbat is not None:
            test_load_energy_h0_published_unscaled_with_source(my_predbat)

        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED")
        print("=" * 60)
        return 0
    except AssertionError as e:
        print("\n" + "=" * 60)
        print(f"❌ TEST FAILED: {e}")
        print("=" * 60)
        return 1
    except Exception as e:
        print("\n" + "=" * 60)
        print(f"❌ ERROR: {e}")
        import traceback

        traceback.print_exc()
        print("=" * 60)
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
