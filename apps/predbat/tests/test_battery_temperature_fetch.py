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
Battery temperature history is fetched for only the window the prediction reads.

predict_battery_temperature() looks back at most 24 hours plus one step, so holding
max_days_previous days of per-minute temperatures (about 3 MB at 22 days) was dead weight.
"""

from datetime import timedelta

from const import PREDICT_STEP, TIME_FORMAT
from fetch import BATTERY_TEMPERATURE_HISTORY_DAYS

TEMP_SENSOR = "sensor.battery_temperature_test"


def _temperature_history(now_utc, days):
    """
    HA-style history for TEMP_SENSOR covering the requested days, one record every 30 minutes,
    oldest first, with a temperature that changes through the day so the trend lookup matters.
    """
    records = []
    for minutes_ago in range(days * 24 * 60, -1, -30):
        stamp = now_utc - timedelta(minutes=minutes_ago)
        temperature = 15.0 + (stamp.hour % 8) * 0.5
        records.append({"state": str(temperature), "last_updated": stamp.strftime(TIME_FORMAT), "attributes": {}})
    return [records]


def test_battery_temperature_fetch(my_predbat):
    """
    fetch_battery_temperature() keeps only BATTERY_TEMPERATURE_HISTORY_DAYS of history yet gives the
    same prediction as the full max_days_previous window did.
    """
    print("\n*** Test: battery temperature history fetched for the prediction window only ***")
    failed = False
    requested_days = []

    def fake_history(entity_id, days=30, required=True, tracked=True):
        """Return history for the temperature sensor, recording the window asked for."""
        if entity_id != TEMP_SENSOR:
            return None
        requested_days.append(days)
        return _temperature_history(my_predbat.now_utc, days)

    saved_history = my_predbat.get_history_wrapper
    saved_arg = my_predbat.args.get("battery_temperature_history", None)
    saved_days = my_predbat.max_days_previous
    my_predbat.get_history_wrapper = fake_history
    my_predbat.args["battery_temperature_history"] = TEMP_SENSOR
    my_predbat.max_days_previous = 8
    try:
        # What the old code computed: the whole max_days_previous window
        full_history = my_predbat.minute_data_import_export(my_predbat.max_days_previous, my_predbat.now_utc, "battery_temperature_history", scale=1.0, increment=False, smoothing=False)
        expected_prediction = my_predbat.predict_battery_temperature(full_history, step=PREDICT_STEP)
        requested_days.clear()

        my_predbat.fetch_battery_temperature()
        history = my_predbat.battery_temperature_history
        prediction = my_predbat.battery_temperature_prediction
    finally:
        my_predbat.get_history_wrapper = saved_history
        my_predbat.max_days_previous = saved_days
        if saved_arg is None:
            my_predbat.args.pop("battery_temperature_history", None)
        else:
            my_predbat.args["battery_temperature_history"] = saved_arg

    if requested_days != [BATTERY_TEMPERATURE_HISTORY_DAYS]:
        print("ERROR: expected one history fetch of {} days, got {}".format(BATTERY_TEMPERATURE_HISTORY_DAYS, requested_days))
        failed = True
    if not history or max(history) > BATTERY_TEMPERATURE_HISTORY_DAYS * 24 * 60:
        print("ERROR: history should stop at {} days, newest/oldest keys {}".format(BATTERY_TEMPERATURE_HISTORY_DAYS, (min(history), max(history)) if history else None))
        failed = True
    if len(history) >= len(full_history):
        print("ERROR: history should be smaller than the full window, {} vs {} minutes".format(len(history), len(full_history)))
        failed = True
    if prediction != expected_prediction:
        diffs = [minute for minute in expected_prediction if prediction.get(minute) != expected_prediction[minute]]
        print("ERROR: prediction changed at {} minutes, first {}".format(len(diffs), diffs[:5]))
        failed = True
    if history.get(0) != full_history.get(0):
        print("ERROR: current temperature changed, {} vs {}".format(history.get(0), full_history.get(0)))
        failed = True

    if not failed:
        print("✓ {} days fetched ({} minutes kept, was {}), prediction unchanged".format(BATTERY_TEMPERATURE_HISTORY_DAYS, len(history), len(full_history)))
    return failed


def _constant_history(now_utc, days, temperature):
    """HA-style history at one temperature, one record every 30 minutes, oldest first."""
    records = []
    for minutes_ago in range(days * 24 * 60, -1, -30):
        stamp = now_utc - timedelta(minutes=minutes_ago)
        records.append({"state": str(temperature), "last_updated": stamp.strftime(TIME_FORMAT), "attributes": {}})
    return [records]


def test_battery_temperature_fetch_average(my_predbat):
    """
    battery_temperature_history given as a list (one sensor per inverter) is the fleet's average temperature, as the
    plan models one combined battery: not the sum, and a sensor reading exactly 0 (a missing reading) is left out.
    """
    print("\n*** Test: battery temperature history from several sensors is their average ***")
    failed = False
    sensors = {"sensor.battery_temperature_a": 10.0, "sensor.battery_temperature_b": 20.0, "sensor.battery_temperature_missing": 0, "sensor.battery_temperature_missing_2": 0}

    def fake_history(entity_id, days=30, required=True, tracked=True):
        """Each sensor reads its own constant temperature."""
        if entity_id not in sensors:
            return None
        return _constant_history(my_predbat.now_utc, days, sensors[entity_id])

    saved_history = my_predbat.get_history_wrapper
    saved_arg = my_predbat.args.get("battery_temperature_history", None)
    my_predbat.get_history_wrapper = fake_history
    try:
        my_predbat.args["battery_temperature_history"] = ["sensor.battery_temperature_a", "sensor.battery_temperature_b", "sensor.battery_temperature_missing"]
        my_predbat.fetch_battery_temperature()
        averaged = dict(my_predbat.battery_temperature_history)
        my_predbat.args["battery_temperature_history"] = ["sensor.battery_temperature_missing"]
        my_predbat.fetch_battery_temperature()
        only_zero = dict(my_predbat.battery_temperature_history)
        my_predbat.args["battery_temperature_history"] = ["sensor.battery_temperature_missing", "sensor.battery_temperature_missing_2"]
        my_predbat.fetch_battery_temperature()
        all_zero = dict(my_predbat.battery_temperature_history)
        my_predbat.args["battery_temperature_history"] = "sensor.battery_temperature_a"
        my_predbat.fetch_battery_temperature()
        single = dict(my_predbat.battery_temperature_history)
    finally:
        my_predbat.get_history_wrapper = saved_history
        if saved_arg is None:
            my_predbat.args.pop("battery_temperature_history", None)
        else:
            my_predbat.args["battery_temperature_history"] = saved_arg

    if not averaged or any(abs(value - 15.0) > 1e-6 for value in averaged.values()):
        print("ERROR: expected the average 15°C at every minute, got {}".format(sorted(set(averaged.values()))[:5]))
        failed = True
    if not only_zero or any(value != 0 for value in only_zero.values()):
        print("ERROR: a lone sensor reading 0 should still read 0 (so the stuck warning sees it), got {}".format(sorted(set(only_zero.values()))[:5]))
        failed = True
    if not all_zero or any(value != 0 for value in all_zero.values()):
        print("ERROR: sensors that all read 0 should read 0 (the learners ignore it later), got {}".format(sorted(set(all_zero.values()))[:5]))
        failed = True
    if not single or any(abs(value - 10.0) > 1e-6 for value in single.values()):
        print("ERROR: a single sensor should read as it is, got {}".format(sorted(set(single.values()))[:5]))
        failed = True
    if not failed:
        print("✓ several sensors averaged, a missing one left out, all-zero kept as 0, a single sensor unchanged")
    return failed


def test_battery_temperature_history_stuck_warning(my_predbat):
    """
    check_battery_temperature_history_stuck() warns only when the last day reads exactly 0, and
    names the GivTCP 3.6.13 replacement when the old battery stack sensor is configured (GH#6193).
    Each case starts with the warn-once state cleared.
    """
    print("\n*** Test: battery temperature history stuck at 0 warns ***")
    failed = False
    old_sensor = "sensor.givtcp_battery_stack_1_bms_temperature"
    day = 24 * 60
    zero_two_days = {minute: 0 for minute in range(0, 2 * day)}
    cases = [
        # (description, entity, history, expect warning, expect rename hint)
        ("old GivTCP sensor stuck at 0", old_sensor, zero_two_days, True, True),
        ("other sensor stuck at 0", TEMP_SENSOR, zero_two_days, True, False),
        ("old GivTCP sensor reading real temperatures", old_sensor, {minute: 18.5 for minute in range(0, 2 * day)}, False, False),
        ("a single non-zero reading", old_sensor, {**zero_two_days, 600: 0.5}, False, False),
        ("died a day ago, real readings before", old_sensor, {minute: (0 if minute < day else 18.0) for minute in range(0, 2 * day)}, True, True),
        ("died under a day ago", old_sensor, {minute: (0 if minute < day - 60 else 18.0) for minute in range(0, 2 * day)}, False, False),
        ("no history", old_sensor, {}, False, False),
    ]

    saved_log = my_predbat.log
    saved_arg = my_predbat.args.get("battery_temperature_history", None)
    saved_warned = my_predbat.battery_temperature_stuck_warned
    try:
        for description, entity, history, expect_warning, expect_hint in cases:
            messages = []
            my_predbat.log = messages.append
            my_predbat.args["battery_temperature_history"] = entity
            my_predbat.battery_temperature_stuck_warned = None
            my_predbat.check_battery_temperature_history_stuck(history)
            warnings = [message for message in messages if message.startswith("Warn:") and "battery_temperature_history" in message]
            if bool(warnings) != expect_warning:
                print("ERROR: {}: expected warning {}, got {}".format(description, expect_warning, messages))
                failed = True
                continue
            if warnings and entity not in warnings[0]:
                print("ERROR: {}: warning does not name the sensor: {}".format(description, warnings[0]))
                failed = True
            has_hint = bool(warnings) and "sensor.givtcp_battery_stack_1_battery_temperature" in warnings[0]
            if has_hint != expect_hint:
                print("ERROR: {}: expected rename hint {}, got {}".format(description, expect_hint, warnings))
                failed = True
    finally:
        my_predbat.log = saved_log
        my_predbat.battery_temperature_stuck_warned = saved_warned
        if saved_arg is None:
            my_predbat.args.pop("battery_temperature_history", None)
        else:
            my_predbat.args["battery_temperature_history"] = saved_arg

    if not failed:
        print("✓ stuck-at-0 warning fires only on a dead sensor, with the GivTCP rename hint on the old name")
    return failed


def test_battery_temperature_history_stuck_warning_fetch(my_predbat):
    """
    Through the real fetch path: HA history that read 18 °C until 25 hours ago and 0 since (one state
    change, as the recorder stores it) warns with the GivTCP rename hint; one that died 6 hours ago does
    not yet; one reading 18 throughout does not; and a sensor whose only record is a 0 from 6 hours ago
    warns at once, because minute_data pads that record back across the whole window.
    """
    print("\n*** Test: battery temperature stuck warning through fetch_battery_temperature() ***")
    failed = False
    old_sensor = "sensor.givtcp_battery_stack_1_bms_temperature"

    def history_with_change(change_minutes_ago, new_value):
        """HA history: 18 °C from 2 days ago, then new_value from change_minutes_ago."""
        now = my_predbat.now_utc
        start = now - timedelta(days=BATTERY_TEMPERATURE_HISTORY_DAYS)
        change = now - timedelta(minutes=change_minutes_ago)
        return [
            [
                {"state": "18.0", "last_updated": start.strftime(TIME_FORMAT), "attributes": {}},
                {"state": str(new_value), "last_updated": change.strftime(TIME_FORMAT), "attributes": {}},
            ]
        ]

    def history_only_zero(minutes_ago):
        """HA history: a single 0 record from minutes_ago, nothing before it (a new or renamed sensor)."""
        stamp = my_predbat.now_utc - timedelta(minutes=minutes_ago)
        return [[{"state": "0", "last_updated": stamp.strftime(TIME_FORMAT), "attributes": {}}]]

    cases = [
        ("dead for 25 hours", lambda: history_with_change(25 * 60, 0), True),
        ("dead for 6 hours", lambda: history_with_change(6 * 60, 0), False),
        ("live at 18 °C", lambda: history_with_change(25 * 60, 18.0), False),
        ("only a 0 record from 6 hours ago", lambda: history_only_zero(6 * 60), True),
    ]
    saved_history = my_predbat.get_history_wrapper
    saved_log = my_predbat.log
    saved_arg = my_predbat.args.get("battery_temperature_history", None)
    saved_warned = my_predbat.battery_temperature_stuck_warned
    try:
        my_predbat.args["battery_temperature_history"] = old_sensor
        for description, build, expect_warning in cases:
            messages = []
            my_predbat.log = messages.append
            my_predbat.battery_temperature_stuck_warned = None
            my_predbat.get_history_wrapper = lambda entity_id, days=30, required=True, tracked=True, build=build: build() if entity_id == old_sensor else None
            my_predbat.fetch_battery_temperature()
            warnings = [message for message in messages if message.startswith("Warn:") and "battery_temperature_history" in message]
            if bool(warnings) != expect_warning:
                print("ERROR: {}: expected warning {}, got {}".format(description, expect_warning, messages))
                failed = True
            elif warnings and "sensor.givtcp_battery_stack_1_battery_temperature" not in warnings[0]:
                print("ERROR: {}: warning lacks the rename hint: {}".format(description, warnings[0]))
                failed = True
    finally:
        my_predbat.get_history_wrapper = saved_history
        my_predbat.log = saved_log
        my_predbat.battery_temperature_stuck_warned = saved_warned
        if saved_arg is None:
            my_predbat.args.pop("battery_temperature_history", None)
        else:
            my_predbat.args["battery_temperature_history"] = saved_arg

    if not failed:
        print("✓ fetch path warns on a sensor dead for over a day or with only 0 history, not on a live or recently dead one")
    return failed


def test_battery_temperature_history_stuck_warning_once(my_predbat):
    """
    The stuck warning is reported once per sensor, re-armed when the sensor reads a real temperature,
    and only claims a plan effect when a battery temperature curve is set (GH#6193 review).
    """
    print("\n*** Test: battery temperature stuck warning is reported once and names the plan effect correctly ***")
    failed = False
    day = 24 * 60
    stuck = {minute: 0 for minute in range(0, 2 * day)}
    live = {minute: 18.0 for minute in range(0, 2 * day)}
    saved = (my_predbat.log, my_predbat.args.get("battery_temperature_history", None), my_predbat.battery_temperature_stuck_warned, my_predbat.battery_temperature_charge_curve, my_predbat.battery_temperature_discharge_curve)
    messages = []

    def warnings_after(history):
        """Run the check and return the stuck warnings it logged."""
        messages.clear()
        my_predbat.check_battery_temperature_history_stuck(history)
        return [message for message in messages if "battery_temperature_history sensor" in message]

    try:
        my_predbat.log = messages.append
        my_predbat.args["battery_temperature_history"] = TEMP_SENSOR
        my_predbat.battery_temperature_stuck_warned = None
        my_predbat.battery_temperature_charge_curve = {}
        my_predbat.battery_temperature_discharge_curve = {}

        first = warnings_after(stuck)
        if len(first) != 1 or "if you set a battery temperature curve" not in first[0]:
            print("ERROR: with no curve, expected one warning saying a curve would be affected, got {}".format(first))
            failed = True
        repeat = warnings_after(stuck)
        if repeat:
            print("ERROR: a second cycle with the same stuck sensor warned again: {}".format(repeat))
            failed = True
        if warnings_after(live):
            print("ERROR: a live sensor warned")
            failed = True
        my_predbat.battery_temperature_charge_curve = {0: 0.3, 20: 0.5}
        rearmed = warnings_after(stuck)
        if len(rearmed) != 1 or "planning at the 0 °C rates" not in rearmed[0]:
            print("ERROR: after recovering and dying again with a curve set, expected one warning naming the 0 °C rates, got {}".format(rearmed))
            failed = True
    finally:
        my_predbat.log = saved[0]
        if saved[1] is None:
            my_predbat.args.pop("battery_temperature_history", None)
        else:
            my_predbat.args["battery_temperature_history"] = saved[1]
        my_predbat.battery_temperature_stuck_warned = saved[2]
        my_predbat.battery_temperature_charge_curve = saved[3]
        my_predbat.battery_temperature_discharge_curve = saved[4]

    if not failed:
        print("✓ reported once, re-armed on recovery, plan effect named only when a curve is set")
    return failed
