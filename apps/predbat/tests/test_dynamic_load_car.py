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
Dynamic load car-not-charging detection.

With metric_dynamic_load_adjust on, a car inside one of its charging slots that is not actually
charging has every future slot cancelled (kwh 0, which releases "Hold for car" and the predicted car
load) and, for an Octopus Intelligent dispatch, loses the dispatch's cheap rate too - so the house
battery is not planned around a slot Octopus may bill at the full rate. The cancellation is
re-derived every cycle and ends as soon as the car leaves its slots or starts charging again.

Evidence is car_charging_now when it is a real entity (2 minute grace), otherwise the low-load test
when the car is inside the CT clamp (10 minute grace), otherwise nothing.
"""
import copy
from datetime import timedelta

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
SENSOR = "binary_sensor.car_charging_now_test"

STATE_FIELDS = (
    "num_cars",
    "minutes_now",
    "now_utc_real",
    "car_charging_slots",
    "car_energy_reported_load",
    "car_charging_threshold",
    "battery_rate_max_discharge",
    "load_last_period",
    "metric_dynamic_load_adjust",
    "octopus_slots",
    "io_adjusted",
    "rate_max_base",
    "rate_min_base",
    "forecast_minutes",
    "update_pending",
    "dynamic_load_car_since",
    "dynamic_load_car_cancelled",
    "dynamic_load_car_decided",
)


def _slots():
    """
    Fresh car slots as the fetch would rebuild them each cycle: one in progress from 14:00 and a
    later one from 15:00.
    """
    return [{"start": 840, "end": 870, "kwh": 3.5, "octopus": True}, {"start": 900, "end": 930, "kwh": 3.5, "octopus": True}]


def _at(my_predbat, minute, seconds=0):
    """
    Move the clock to minute past midnight (plus seconds), as update_time() would: minutes_now is
    floored to the 5 minute PREDICT_STEP while now_utc_real stays exact.
    """
    my_predbat.now_utc_real = my_predbat.midnight_utc + timedelta(minutes=minute, seconds=seconds)
    my_predbat.minutes_now = int(minute / 5) * 5
    return my_predbat.now_utc_real


def _cycle(my_predbat, minute, seconds=0, slots=None, save=True):
    """
    One fetch cycle for car 0: rebuild its slots, then run the early (pre-rates) check.
    """
    _at(my_predbat, minute, seconds)
    my_predbat.car_charging_slots = [copy.deepcopy(slots if slots is not None else _slots())] + [[] for _ in range(my_predbat.num_cars - 1)]
    return my_predbat.dynamic_load_car_check(save=save)


def _sensor(my_predbat, state):
    """
    Point car_charging_now at a real entity reporting state, or remove the entity when state is None.
    """
    if state is None:
        my_predbat.args.pop("car_charging_now", None)
        return
    my_predbat.args["car_charging_now"] = SENSOR
    my_predbat.ha_interface.dummy_items[SENSOR] = state


def _reset(my_predbat):
    """
    A single car with dynamic load on and no detection state carried over.
    """
    my_predbat.num_cars = 1
    my_predbat.metric_dynamic_load_adjust = True
    my_predbat.car_energy_reported_load = False
    my_predbat.car_charging_threshold = 3.0 / 60.0
    my_predbat.battery_rate_max_discharge = 5.0 / 60.0
    my_predbat.load_last_period = 3.0
    my_predbat.dynamic_load_car_since = {}
    my_predbat.dynamic_load_car_cancelled = {}
    my_predbat.dynamic_load_car_decided = set()
    my_predbat.update_pending = False


def _kwh(my_predbat):
    """
    kWh of car 0's slots after the check.
    """
    return [slot["kwh"] for slot in my_predbat.car_charging_slots[0]]


def _check(name, condition, detail=""):
    """
    Print an error for a failed condition and return whether it failed.
    """
    if not condition:
        print("ERROR: {} {}".format(name, detail))
        return True
    return False


def _run(my_predbat):
    """
    The individual scenarios.
    """
    failed = False
    saved_sensor = my_predbat.args.get("car_charging_now", None)
    had_sensor = "car_charging_now" in my_predbat.args

    try:
        # Sensor: two minutes of "not charging" inside a slot cancels this and every later slot
        print("Test 1: car_charging_now off for 2 minutes cancels the slots")
        _reset(my_predbat)
        _sensor(my_predbat, "off")
        changed = _cycle(my_predbat, 840, 10)
        failed |= _check("t1 first sighting", not changed and _kwh(my_predbat) == [3.5, 3.5], "changed {} kwh {}".format(changed, _kwh(my_predbat)))
        changed = _cycle(my_predbat, 841, 30)
        failed |= _check("t1 inside grace", not changed and _kwh(my_predbat) == [3.5, 3.5], "changed {} kwh {}".format(changed, _kwh(my_predbat)))
        changed = _cycle(my_predbat, 842, 10)
        failed |= _check("t1 after grace", changed and _kwh(my_predbat) == [0, 0], "changed {} kwh {}".format(changed, _kwh(my_predbat)))
        changed = _cycle(my_predbat, 845)
        failed |= _check("t1 stays cancelled without a second replan", (not changed) and _kwh(my_predbat) == [0, 0], "changed {} kwh {}".format(changed, _kwh(my_predbat)))

        # Charging again resets straight away
        print("Test 2: car_charging_now on again resumes the slots")
        _sensor(my_predbat, "on")
        changed = _cycle(my_predbat, 847)
        failed |= _check("t2 resume", changed and _kwh(my_predbat) == [3.5, 3.5] and not my_predbat.dynamic_load_car_cancelled.get(0, False), "changed {} kwh {}".format(changed, _kwh(my_predbat)))

        # Leaving the slots resets, even though the car is still not charging
        print("Test 3: leaving the car slots resets the cancellation")
        _sensor(my_predbat, "off")
        _cycle(my_predbat, 850)
        _cycle(my_predbat, 855)
        failed |= _check("t3 cancelled first", _kwh(my_predbat) == [0, 0], "kwh {}".format(_kwh(my_predbat)))
        changed = _cycle(my_predbat, 875)
        failed |= _check("t3 reset outside slot", changed and _kwh(my_predbat) == [3.5, 3.5] and not my_predbat.dynamic_load_car_cancelled.get(0, False), "changed {} kwh {}".format(changed, _kwh(my_predbat)))
        failed |= _check("t3 since cleared", my_predbat.dynamic_load_car_since.get(0) is None, "since {}".format(my_predbat.dynamic_load_car_since.get(0)))

        # Unknown readings neither start the clock nor clear a cancellation
        print("Test 4: unavailable car_charging_now is no evidence either way")
        _reset(my_predbat)
        _sensor(my_predbat, "unavailable")
        _cycle(my_predbat, 840)
        _cycle(my_predbat, 850)
        failed |= _check("t4 no cancel on unknown", _kwh(my_predbat) == [3.5, 3.5] and my_predbat.dynamic_load_car_since.get(0) is None, "kwh {}".format(_kwh(my_predbat)))
        _sensor(my_predbat, "off")
        _cycle(my_predbat, 850)
        _cycle(my_predbat, 855)
        _sensor(my_predbat, "unavailable")
        changed = _cycle(my_predbat, 860)
        failed |= _check("t4 unknown keeps cancellation", (not changed) and _kwh(my_predbat) == [0, 0], "changed {} kwh {}".format(changed, _kwh(my_predbat)))

        # A static literal is not a sensor, and without the CT clamp there is no load evidence either
        # A configured entity HA does not have (deleted, renamed, integration reloading) resolves to the
        # read's default - that must be no evidence, not a "not charging" reading (#5229 review)
        print("Test 4b: a car_charging_now entity missing from HA is no evidence")
        _reset(my_predbat)
        my_predbat.args["car_charging_now"] = "binary_sensor.car_charging_now_missing"
        my_predbat.ha_interface.dummy_items.pop("binary_sensor.car_charging_now_missing", None)
        _cycle(my_predbat, 840)
        _cycle(my_predbat, 850)
        failed |= _check("t4b missing entity", _kwh(my_predbat) == [3.5, 3.5] and my_predbat.dynamic_load_car_since.get(0) is None, "kwh {} since {}".format(_kwh(my_predbat), my_predbat.dynamic_load_car_since.get(0)))

        print("Test 5: a literal car_charging_now is not evidence")
        _reset(my_predbat)
        my_predbat.args["car_charging_now"] = "off"
        my_predbat.load_last_period = 0.2
        _cycle(my_predbat, 840)
        _cycle(my_predbat, 860)
        failed |= _check("t5 literal ignored", _kwh(my_predbat) == [3.5, 3.5], "kwh {}".format(_kwh(my_predbat)))

        # Load method: only when the car is inside the CT clamp, and only after 10 minutes
        print("Test 6: low load inside the CT clamp cancels after 10 minutes")
        _reset(my_predbat)
        _sensor(my_predbat, None)
        my_predbat.car_energy_reported_load = True
        my_predbat.load_last_period = 0.5
        _cycle(my_predbat, 840)
        changed = _cycle(my_predbat, 845)
        failed |= _check("t6 not at 5 minutes", (not changed) and _kwh(my_predbat) == [3.5, 3.5], "changed {} kwh {}".format(changed, _kwh(my_predbat)))
        changed = _cycle(my_predbat, 850)
        failed |= _check("t6 cancels at 10 minutes", changed and _kwh(my_predbat) == [0, 0], "changed {} kwh {}".format(changed, _kwh(my_predbat)))
        my_predbat.load_last_period = 4.0
        changed = _cycle(my_predbat, 855)
        failed |= _check("t6 load back up resumes", changed and _kwh(my_predbat) == [3.5, 3.5], "changed {} kwh {}".format(changed, _kwh(my_predbat)))

        print("Test 6b: the load test is not used just after midnight, when load_today resets")
        _reset(my_predbat)
        _sensor(my_predbat, None)
        my_predbat.car_energy_reported_load = True
        my_predbat.load_last_period = 0.5
        early_slots = [{"start": 0, "end": 30, "kwh": 3.5}]
        _cycle(my_predbat, 0, slots=early_slots)
        _cycle(my_predbat, 5, slots=early_slots)
        failed |= _check("t6b no clock before 00:05", my_predbat.dynamic_load_car_since.get(0) is None, "since {}".format(my_predbat.dynamic_load_car_since.get(0)))

        print("Test 7: low load with the car outside the CT clamp is not evidence")
        _reset(my_predbat)
        _sensor(my_predbat, None)
        my_predbat.car_energy_reported_load = False
        my_predbat.load_last_period = 0.5
        _cycle(my_predbat, 840)
        _cycle(my_predbat, 860)
        failed |= _check("t7 no load evidence outside CT", _kwh(my_predbat) == [3.5, 3.5], "kwh {}".format(_kwh(my_predbat)))

        print("Test 8: a wired sensor wins over the load test")
        _reset(my_predbat)
        _sensor(my_predbat, "on")
        my_predbat.car_energy_reported_load = True
        my_predbat.load_last_period = 0.5
        _cycle(my_predbat, 840)
        _cycle(my_predbat, 860)
        failed |= _check("t8 sensor says charging", _kwh(my_predbat) == [3.5, 3.5], "kwh {}".format(_kwh(my_predbat)))

        print("Test 9: dynamic load off does nothing and clears state")
        _reset(my_predbat)
        _sensor(my_predbat, "off")
        _cycle(my_predbat, 840)
        _cycle(my_predbat, 845)
        my_predbat.metric_dynamic_load_adjust = False
        changed = _cycle(my_predbat, 850)
        failed |= _check("t9 off", changed and _kwh(my_predbat) == [3.5, 3.5] and not my_predbat.dynamic_load_car_cancelled.get(0, False), "changed {} kwh {}".format(changed, _kwh(my_predbat)))

        print("Test 10: compare.py runs (save=False) apply the cancellation but do not advance it")
        _reset(my_predbat)
        _sensor(my_predbat, "off")
        _cycle(my_predbat, 840, save=False)
        failed |= _check("t10 no clock started", my_predbat.dynamic_load_car_since.get(0) is None, "since {}".format(my_predbat.dynamic_load_car_since.get(0)))
        _cycle(my_predbat, 840)
        _cycle(my_predbat, 845)
        _cycle(my_predbat, 850, save=False)
        failed |= _check("t10 applied when saved state is cancelled", _kwh(my_predbat) == [0, 0], "kwh {}".format(_kwh(my_predbat)))
        # A car that has since resumed charging, or left its slot, is not cancelled in a comparison run
        # just because the live state has not been re-derived yet (#5229 review)
        _sensor(my_predbat, "on")
        _cycle(my_predbat, 851, save=False)
        failed |= _check("t10 not applied once charging again", _kwh(my_predbat) == [3.5, 3.5], "kwh {}".format(_kwh(my_predbat)))
        _sensor(my_predbat, "off")
        _cycle(my_predbat, 875, save=False)
        failed |= _check("t10 not applied outside the slot", _kwh(my_predbat) == [3.5, 3.5], "kwh {}".format(_kwh(my_predbat)))
        failed |= _check("t10 saved state untouched", my_predbat.dynamic_load_car_cancelled.get(0, False), "cancelled {}".format(my_predbat.dynamic_load_car_cancelled))

        # Cars planned by Predbat itself get their slots after the rates are built: the late call
        # decides the cars the early call could not
        print("Test 11: the late call decides cars that had no slots at the early call")
        _reset(my_predbat)
        _sensor(my_predbat, "off")
        _at(my_predbat, 840)
        my_predbat.car_charging_slots = [[]]
        my_predbat.dynamic_load_car_check()
        my_predbat.car_charging_slots = [_slots()]
        my_predbat.dynamic_load_car_check(late=True)
        _at(my_predbat, 845)
        my_predbat.car_charging_slots = [[]]
        my_predbat.dynamic_load_car_check()
        my_predbat.car_charging_slots = [_slots()]
        changed = my_predbat.dynamic_load_car_check(late=True)
        failed |= _check("t11 late cancel", changed and _kwh(my_predbat) == [0, 0], "changed {} kwh {}".format(changed, _kwh(my_predbat)))

        # car_charging_slots shorter than num_cars must not raise out of the fetch (#5229 review)
        print("Test 11b: fewer car slot lists than cars")
        _reset(my_predbat)
        _sensor(my_predbat, "off")
        _at(my_predbat, 840)
        my_predbat.num_cars = 2
        my_predbat.dynamic_load_car_cancelled = {1: True}
        my_predbat.car_charging_slots = [_slots()]
        try:
            my_predbat.dynamic_load_car_check(save=False, late=True)
            my_predbat.dynamic_load_car_cancelled = {1: True}
            my_predbat.dynamic_load_car_check()
            my_predbat.dynamic_load_car_check(late=True)
        except IndexError as exc:
            failed |= _check("t11b no IndexError", False, str(exc))

        failed |= _run_rates(my_predbat)
        failed |= _run_poll(my_predbat)
    finally:
        if had_sensor:
            my_predbat.args["car_charging_now"] = saved_sensor
        else:
            my_predbat.args.pop("car_charging_now", None)
        my_predbat.ha_interface.dummy_items.pop(SENSOR, None)

    return failed


def _dispatch(my_predbat, start_minute, end_minute):
    """
    A raw Octopus dispatch entry covering start_minute to end_minute past midnight.
    """
    start = my_predbat.midnight_utc + timedelta(minutes=start_minute)
    end = my_predbat.midnight_utc + timedelta(minutes=end_minute)
    return {"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "charge_in_kwh": 2.0, "source": "smart-charge", "location": "AT_HOME"}


def _run_rates(my_predbat):
    """
    The cheap rate of a cancelled car's dispatch is never applied, and a discount the rate feed
    delivered for it is stripped before the rates are replicated.
    """
    failed = False
    saved_low_rate = my_predbat.args.get("octopus_slot_low_rate", None)
    saved_slot_max = my_predbat.args.get("octopus_slot_max", None)
    try:
        print("Test 12: rate_add_io_slots skips the future minutes of a cancelled car")
        _reset(my_predbat)
        _at(my_predbat, 840)
        my_predbat.forecast_minutes = 2 * 24 * 60
        my_predbat.args["octopus_slot_low_rate"] = True
        my_predbat.args["octopus_slot_max"] = 48
        my_predbat.rate_min_base = 4.0
        my_predbat.rate_max_base = 30.0
        my_predbat.dynamic_load_car_cancelled = {0: True}
        rates = {minute: 10.0 for minute in range(0, 2 * 24 * 60)}
        rates = my_predbat.rate_add_io_slots(0, rates, [_dispatch(my_predbat, 810, 900)])
        failed |= _check("t12 past minute keeps its discount", rates[820] == 4.0, "rate {}".format(rates[820]))
        failed |= _check("t12 future minute not discounted", rates[840] == 10.0 and rates[899] == 10.0, "rates {} {}".format(rates[840], rates[899]))

        my_predbat.dynamic_load_car_cancelled = {}
        rates = {minute: 10.0 for minute in range(0, 2 * 24 * 60)}
        rates = my_predbat.rate_add_io_slots(0, rates, [_dispatch(my_predbat, 810, 900)])
        failed |= _check("t12 not cancelled is discounted", rates[840] == 4.0, "rate {}".format(rates[840]))

        print("Test 13: a feed-side discount for a cancelled car's dispatch is stripped")
        _reset(my_predbat)
        _at(my_predbat, 840)
        my_predbat.num_cars = 2
        my_predbat.rate_max_base = 30.0
        my_predbat.dynamic_load_car_cancelled = {0: True}
        my_predbat.octopus_slots = [[_dispatch(my_predbat, 810, 930), _dispatch(my_predbat, 1410, 1440)], [_dispatch(my_predbat, 900, 930)]]
        rates = {minute: 25.0 for minute in range(0, 2 * 24 * 60)}
        my_predbat.io_adjusted = {}
        for minute in list(range(810, 930)) + list(range(1410, 1440)):
            rates[minute] = 7.0
            my_predbat.io_adjusted[minute] = True
        rates = my_predbat.dynamic_load_car_strip_feed_rates(rates)
        failed |= _check("t13 future stripped", rates[850] == 30.0 and 850 not in my_predbat.io_adjusted, "rate {} marker {}".format(rates[850], my_predbat.io_adjusted.get(850)))
        failed |= _check("t13 past kept", rates[820] == 7.0 and my_predbat.io_adjusted.get(820), "rate {}".format(rates[820]))
        failed |= _check("t13 other car's dispatch kept", rates[910] == 7.0 and my_predbat.io_adjusted.get(910), "rate {}".format(rates[910]))
        failed |= _check("t13 fixed window kept", rates[1420] == 7.0 and my_predbat.io_adjusted.get(1420), "rate {}".format(rates[1420]))

        # The feed marks whole 30 minute rate periods, and rate_add_io_slots() rounds a dispatch out to
        # them too - a dispatch off the half hour must strip the whole period (#5229 review)
        print("Test 13b: a dispatch off the half hour strips its whole 30 minute periods")
        my_predbat.octopus_slots = [[_dispatch(my_predbat, 967, 997)], []]
        my_predbat.io_adjusted = {}
        for minute in range(960, 1020):
            rates[minute] = 7.0
            my_predbat.io_adjusted[minute] = True
        rates = my_predbat.dynamic_load_car_strip_feed_rates(rates)
        failed |= _check("t13b leading edge stripped", rates[962] == 30.0, "rate {}".format(rates[962]))
        failed |= _check("t13b trailing edge stripped", rates[1010] == 30.0, "rate {}".format(rates[1010]))
        my_predbat.octopus_slots = [[_dispatch(my_predbat, 810, 930), _dispatch(my_predbat, 1410, 1440)], [_dispatch(my_predbat, 900, 930)]]

        # A cancelled car's dispatch that runs into the fixed 23:30-05:30 window: the part outside it goes
        # back to the full rate, the part inside keeps the tariff's own off-peak rate through both the
        # feed strip and rate_add_io_slots() - which only ever lowers a rate, so skipping it changes nothing
        print("Test 13c: a cancelled dispatch straddling 23:30 keeps the off-peak rate inside the window")
        my_predbat.num_cars = 1
        my_predbat.args["octopus_slot_low_rate"] = True
        my_predbat.args["octopus_slot_max"] = 48
        my_predbat.rate_min_base = 4.0
        dispatch = [_dispatch(my_predbat, 1380, 1470)]
        my_predbat.octopus_slots = [dispatch]
        rates = {minute: 25.0 for minute in range(0, 2 * 24 * 60)}
        for minute in list(range(1410, 1440)) + list(range(1440, 1440 + 330)):
            rates[minute] = 7.0
        # Worst case: the feed flags the whole dispatch as adjusted, including the minutes inside the window
        my_predbat.io_adjusted = {}
        for minute in range(1380, 1470):
            rates[minute] = 7.0
            my_predbat.io_adjusted[minute] = True
        rates = my_predbat.dynamic_load_car_strip_feed_rates(rates)
        rates = my_predbat.rate_add_io_slots(0, rates, dispatch)
        failed |= _check("t13c 23:00-23:30 back to the full rate", rates[1380] == 30.0 and rates[1409] == 30.0, "rates {} {}".format(rates[1380], rates[1409]))
        failed |= _check("t13c 23:30-00:30 still off-peak", all(rates[minute] == 7.0 for minute in range(1410, 1470)), "rates {}".format(sorted(set(rates[minute] for minute in range(1410, 1470)))))
        my_predbat.num_cars = 2
        my_predbat.octopus_slots = [[_dispatch(my_predbat, 810, 930), _dispatch(my_predbat, 1410, 1440)], [_dispatch(my_predbat, 900, 930)]]

        my_predbat.dynamic_load_car_cancelled = {}
        rates[850] = 7.0
        my_predbat.io_adjusted[850] = True
        rates = my_predbat.dynamic_load_car_strip_feed_rates(rates)
        failed |= _check("t13 nothing cancelled is a no-op", rates[850] == 7.0 and my_predbat.io_adjusted.get(850), "rate {}".format(rates[850]))
    finally:
        for key, value in (("octopus_slot_low_rate", saved_low_rate), ("octopus_slot_max", saved_slot_max)):
            if value is None:
                my_predbat.args.pop(key, None)
            else:
                my_predbat.args[key] = value
    return failed


def _run_poll(my_predbat):
    """
    The 15 second loop asks for a replan as soon as a cancel or resume is due, rather than waiting
    for the next 5 minute cycle.
    """
    failed = False
    print("Test 14: the poll asks for a replan once the grace has run out")
    _reset(my_predbat)
    _sensor(my_predbat, "off")
    _cycle(my_predbat, 840, 5)
    due = my_predbat.dynamic_load_car_poll(now=my_predbat.midnight_utc + timedelta(minutes=841))
    failed |= _check("t14 not yet due", (not due) and not my_predbat.update_pending, "due {}".format(due))
    due = my_predbat.dynamic_load_car_poll(now=my_predbat.midnight_utc + timedelta(minutes=842, seconds=10))
    failed |= _check("t14 due after grace", due and my_predbat.update_pending, "due {}".format(due))

    print("Test 15: the poll starts the clock itself when the slot began between cycles")
    _reset(my_predbat)
    _sensor(my_predbat, "off")
    _cycle(my_predbat, 835)
    due = my_predbat.dynamic_load_car_poll(now=my_predbat.midnight_utc + timedelta(minutes=840, seconds=20))
    failed |= _check("t15 clock started", (not due) and my_predbat.dynamic_load_car_since.get(0) is not None, "due {} since {}".format(due, my_predbat.dynamic_load_car_since.get(0)))
    due = my_predbat.dynamic_load_car_poll(now=my_predbat.midnight_utc + timedelta(minutes=842, seconds=30))
    failed |= _check("t15 due", due, "due {}".format(due))

    print("Test 16: the poll asks for a replan when a cancelled car starts charging")
    _reset(my_predbat)
    _sensor(my_predbat, "off")
    _cycle(my_predbat, 840)
    _cycle(my_predbat, 845)
    my_predbat.update_pending = False
    _sensor(my_predbat, "on")
    due = my_predbat.dynamic_load_car_poll(now=my_predbat.midnight_utc + timedelta(minutes=846))
    failed |= _check("t16 resume due", due and my_predbat.update_pending, "due {}".format(due))

    # The poll must judge "in a slot" exactly as the replan's check will - on minutes_now, floored to
    # PREDICT_STEP - or it keeps asking for a change the replan then declines, every 15 seconds
    print("Test 18: the poll agrees with the check about a slot ending between 5 minute steps")
    _reset(my_predbat)
    _sensor(my_predbat, "off")
    odd_slots = [{"start": 840, "end": 872, "kwh": 3.5}]
    _cycle(my_predbat, 840, slots=odd_slots)
    _cycle(my_predbat, 845, slots=odd_slots)
    my_predbat.update_pending = False
    due = my_predbat.dynamic_load_car_poll(now=my_predbat.midnight_utc + timedelta(minutes=872, seconds=10))
    failed |= _check("t18 no reset before minutes_now leaves the slot", not due, "due {}".format(due))
    changed = _cycle(my_predbat, 872, 25, slots=odd_slots)
    failed |= _check("t18 check agrees", (not changed) and my_predbat.dynamic_load_car_cancelled.get(0, False), "changed {}".format(changed))
    due = my_predbat.dynamic_load_car_poll(now=my_predbat.midnight_utc + timedelta(minutes=875, seconds=5))
    failed |= _check("t18 reset once minutes_now leaves it", due, "due {}".format(due))

    print("Test 17: the poll does nothing with dynamic load off")
    _reset(my_predbat)
    _sensor(my_predbat, "off")
    _cycle(my_predbat, 840)
    my_predbat.metric_dynamic_load_adjust = False
    due = my_predbat.dynamic_load_car_poll(now=my_predbat.midnight_utc + timedelta(minutes=845))
    failed |= _check("t17 off", not due, "due {}".format(due))
    return failed


def test_dynamic_load_car_not_charging(my_predbat):
    """
    Dynamic load cancels the car slots, and their cheap rate, while a car in its slot is not charging.
    """
    print("*** Running test: Dynamic load car not charging")
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in STATE_FIELDS}
    try:
        failed = _run(my_predbat)
    finally:
        for field, value in saved_state.items():
            setattr(my_predbat, field, value)
    print("*** Dynamic load car not charging test {}".format("FAILED" if failed else "PASSED"))
    return failed
