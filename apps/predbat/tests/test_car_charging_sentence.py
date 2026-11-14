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
The plan-details line about the car right now (GH#3017).

The plan details used to say "Your car is currently charging." whenever the plan had car energy in the current slot,
even when the car's car_charging_now sensor said it was not charging. Now a car whose sensor says it is not charging
is described as planned to charge; without a sensor reading the plan's word stands.
"""

import copy

from tests.test_infra import reset_inverter, reset_rates


def _check(label, got, expected):
    """Print a pass/fail line and return True when the check failed."""
    if got == expected:
        print("  PASS: {}".format(label))
        return False
    print("  ERROR: {}: expected {!r}, got {!r}".format(label, expected, got))
    return True


def _test_fetch_records_reading(my_predbat):
    """get_car_charging_planned() keeps car_charging_now's reading: None without a sensor, False for not charging, True for charging."""
    failed = False
    sensor_set = "car_charging_now" in my_predbat.args
    sensor_before = my_predbat.args.get("car_charging_now")
    num_cars_before = my_predbat.num_cars
    try:
        my_predbat.num_cars = 1
        for label, sensor, expected in (("no sensor", None, None), ("sensor off", "off", False), ("sensor on", "on", True), ("sensor unavailable", "unavailable", None)):
            if sensor is None:
                my_predbat.args.pop("car_charging_now", None)
            else:
                my_predbat.args["car_charging_now"] = sensor
            my_predbat.get_car_charging_planned()
            failed |= _check("fetch reading, {}".format(label), my_predbat.car_charging_now_state[0], expected)
            failed |= _check("fetch car_charging_now, {}".format(label), my_predbat.car_charging_now[0], bool(expected))
    finally:
        my_predbat.num_cars = num_cars_before
        if sensor_set:
            my_predbat.args["car_charging_now"] = sensor_before
        else:
            my_predbat.args.pop("car_charging_now", None)
    return failed


def _test_plan_text_wiring(my_predbat):
    """short_textual_plan() carries the car line from car_charging_now_sentence(), so reverting its call site fails here."""
    failed = False
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    reset_rates(my_predbat, 10.0, 5.0)
    my_predbat.predict_soc_best = {0: 0.0}
    my_predbat.predict_metric_best = {}
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    flat_step = {minute: 0.0 for minute in range(0, my_predbat.forecast_minutes, 5)}
    my_predbat.num_cars = 1
    my_predbat.minutes_now = 610
    my_predbat.car_charging_slots = [[{"start": 600, "end": 660, "kwh": 6.0}]]
    for label, state, expected, unexpected in (
        ("plan text, sensor says not charging", False, "- Your car is planned to charge now.\n", "- Your car is currently charging.\n"),
        ("plan text, no sensor reading", None, "- Your car is currently charging.\n", "- Your car is planned to charge now.\n"),
    ):
        my_predbat.car_charging_now_state = [state]
        my_predbat.car_charging_now = [bool(state)]
        text = my_predbat.short_textual_plan(0.0, 0, flat_step, flat_step, flat_step, flat_step, my_predbat.forecast_minutes, publish=False)
        failed |= _check(label, expected in text and unexpected not in text, True)
    return failed


def test_car_charging_sentence(my_predbat):
    """car_charging_now_sentence() follows car_charging_now, and falls back to plan intent."""
    print("**** Running car charging sentence tests ****")
    fields = ("num_cars", "minutes_now", "car_charging_now", "car_charging_now_state", "car_charging_slots")
    saved = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in fields}
    failed = False
    planned_now = [{"start": 600, "end": 660, "kwh": 6.0}]
    planned_later = [{"start": 900, "end": 960, "kwh": 6.0}]
    cases = [
        # (label, car_charging_now reading per car - True, False or None for no reading - slots per car, expected line)
        ("planned now, sensor says not charging", [False], [planned_now], "- Your car is planned to charge now.\n"),
        ("planned now, no sensor reading", [None], [planned_now], "- Your car is currently charging.\n"),
        ("planned now and charging", [True], [planned_now], "- Your car is currently charging.\n"),
        ("charging with nothing planned", [True], [[]], "- Your car is currently charging.\n"),
        ("planned later, not charging", [False], [planned_later], ""),
        ("no car", [], [], ""),
        ("second car charging, first planned", [False, True], [planned_now, []], "- Your car is currently charging.\n"),
        ("two cars planned, both known not charging", [False, False], [planned_now, planned_now], "- Your car is planned to charge now.\n"),
        ("two cars planned, one known not charging, one no reading", [False, None], [planned_now, planned_now], "- Your car is currently charging.\n"),
        ("a near-empty slot says nothing", [False], [[{"start": 600, "end": 630, "kwh": 0.001}]], ""),
    ]
    fetch_fields = ("car_charging_planned", "car_charging_plan_smart", "car_charging_plan_max_price", "car_charging_plan_time", "car_charging_battery_size", "car_charging_limit", "car_charging_rate", "car_charging_max_rate", "car_charging_exclusive")
    saved.update({field: copy.deepcopy(getattr(my_predbat, field, None)) for field in fetch_fields})
    try:
        failed |= _test_fetch_records_reading(my_predbat)
        for label, states, slots, expected in cases:
            my_predbat.num_cars = len(states)
            my_predbat.minutes_now = 610
            my_predbat.car_charging_now_state = list(states)
            my_predbat.car_charging_now = [bool(state) for state in states]
            my_predbat.car_charging_slots = copy.deepcopy(slots)
            failed |= _check(label, my_predbat.car_charging_now_sentence(), expected)
        failed |= _test_plan_text_wiring(my_predbat)
    finally:
        for field, value in saved.items():
            setattr(my_predbat, field, value)
    return failed
