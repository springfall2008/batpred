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
