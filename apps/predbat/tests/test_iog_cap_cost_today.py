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
The cost of car charging beyond the Octopus Intelligent 6-hour cap in today's cost.

Once a car has had 6 hours of dispatch in a midday-to-midday period, Octopus bills its charging at the peak
rate while the house keeps its own rate. today_cost() adds that difference to the day's cost. It used to add it
for the whole planned energy of an over-cap car slot as soon as the slot had started: the in-progress dispatch's
start is moved up to now every cycle, so the slot had always started, and a dispatch planned to 06:00 put
about 44 kWh of car charging that had not happened yet at the peak rate into the day's cost (+1332p at 23:35 on
the reporter's system, when the car drew 3.4 kWh before midnight).
"""

import copy

from tests.test_octopus_dispatch_hold import STATE_FIELDS, _check, _cycle, _dispatch, _reset

RATE_OFF_PEAK = 6.60
RATE_PEAK = 36.28
CAR_KWH_PER_MINUTE = 0.12  # 7.2 kW
EXTRA_FIELDS = ("rate_export", "soc_kwh_history", "metric_standing_charge", "carbon_enable", "octopus_intelligent_consider_full")


def _friday_dispatches(my_predbat):
    """
    The reporter's Friday: 3.5 hours of dispatches in the afternoon and 2.5 hours from 21:00, which use up the
    6-hour cap at 23:30, as the completed dispatches Octopus lists.
    """
    completed = []
    for start, end in ((750, 780), (810, 840), (870, 1020), (1260, 1410)):
        for half_hour in range(start, end, 30):
            completed.append(_dispatch(my_predbat, half_hour, half_hour + 30, kwh=-3.4, source="unknown"))
    return completed


def _cheap_minutes():
    """
    The minutes the house is billed off-peak: the dispatches inside the cap and the fixed 23:30-05:30 window.
    """
    minutes = set(range(0, 330)) | set(range(1410, 1440))
    for start, end in ((750, 780), (810, 840), (870, 1020), (1260, 1410)):
        minutes |= set(range(start, end))
    return minutes


def _car_minutes():
    """
    The minutes the car drew power: every dispatch minute.
    """
    minutes = set()
    for start, end in ((750, 780), (810, 840), (870, 1020), (1260, 1440)):
        minutes |= set(range(start, end))
    return minutes


def _incrementing(minutes_now, per_minute):
    """
    A kWh-today sensor as minute_data() returns it, keyed by minutes back from now, from the energy in each minute.
    """
    data = {}
    total = 0.0
    totals = {}
    for minute in range(minutes_now + 1):
        totals[minute] = total
        total += per_minute.get(minute, 0.0)
    for minute_back in range(minutes_now + 2):
        data[minute_back] = totals.get(minutes_now - minute_back, 0.0)
    return data


def _setup_friday(my_predbat, minute):
    """
    Friday at minute past midnight with the overnight dispatch running, planned to 06:00 with the energy to fill the
    car, its start advanced to just before now as the API components do. Returns the car's energy in each minute.
    """
    _reset(my_predbat)
    my_predbat.args["octopus_slot_max"] = 12
    my_predbat.args["octopus_slot_low_rate"] = True
    my_predbat.octopus_intelligent_consider_full = False
    my_predbat.metric_standing_charge = 0
    my_predbat.carbon_enable = False
    my_predbat.rate_export = {}
    my_predbat.soc_kwh_history = {}
    my_predbat.rate_min_base = RATE_OFF_PEAK
    my_predbat.rate_max_base = RATE_PEAK
    # A cycle every 5 minutes from 23:00, so the half hours the running dispatch has been seen in are held until
    # Octopus lists them as completed, as they are live
    for cycle_minute in range(1380, minute + 1, 5):
        planned = [_dispatch(my_predbat, cycle_minute - 2, 1800, kwh=-44.7, source="unknown", location=None)]
        _cycle(my_predbat, cycle_minute, planned=planned, completed=_friday_dispatches(my_predbat))
    cheap = _cheap_minutes()
    my_predbat.rate_import = {m: RATE_OFF_PEAK if m in cheap else RATE_PEAK for m in range(-24 * 60, 2 * 24 * 60)}
    return {m: CAR_KWH_PER_MINUTE for m in _car_minutes() if m < my_predbat.minutes_now}


def _expected_cost(my_predbat, car_per_minute, over_cap_from):
    """
    The day's cost when the house imports only what the car draws: every minute at the house rate, plus the gap to
    the peak rate on the car energy drawn from over_cap_from on.
    """
    cost = 0.0
    for minute, kwh in car_per_minute.items():
        cost += kwh * my_predbat.rate_import[minute]
        if minute >= over_cap_from:
            cost += kwh * (RATE_PEAK - my_predbat.rate_import[minute])
    return cost


def _run(my_predbat):
    """
    The day's cost at points through Friday evening.
    """
    failed = False

    print("Test 1: at 23:35, the 5 minutes the car has drawn beyond the cap are at the peak rate, not the 44 kWh planned to 06:00")
    car = _setup_friday(my_predbat, 1415)
    energy = _incrementing(my_predbat.minutes_now, car)
    cost, _ = my_predbat.today_cost(energy, {}, energy, energy, save=False)
    expected = _expected_cost(my_predbat, car, 1410)
    failed |= _check("t1 cost at 23:35", abs(cost - expected) < 0.5, "got {:.2f}p expected {:.2f}p".format(cost, expected))

    print("Test 2: at 23:55, the 25 minutes drawn beyond the cap are at the peak rate")
    car = _setup_friday(my_predbat, 1435)
    energy = _incrementing(my_predbat.minutes_now, car)
    cost, _ = my_predbat.today_cost(energy, {}, energy, energy, save=False)
    expected = _expected_cost(my_predbat, car, 1410)
    failed |= _check("t2 cost at 23:55", abs(cost - expected) < 0.5, "got {:.2f}p expected {:.2f}p".format(cost, expected))

    print("Test 3: at 23:25, inside the cap, the car pays the house rate")
    car = _setup_friday(my_predbat, 1405)
    energy = _incrementing(my_predbat.minutes_now, car)
    cost, _ = my_predbat.today_cost(energy, {}, energy, energy, save=False)
    expected = _expected_cost(my_predbat, car, 1440)
    failed |= _check("t3 cost at 23:25", abs(cost - expected) < 0.5, "got {:.2f}p expected {:.2f}p".format(cost, expected))

    print("Test 4: with no car energy sensor, the dispatch's own energy for the minutes gone beyond the cap is at the peak rate")
    car = _setup_friday(my_predbat, 1435)
    energy = _incrementing(my_predbat.minutes_now, car)
    cost, _ = my_predbat.today_cost(energy, {}, None, energy, save=False)
    house = _expected_cost(my_predbat, car, 1440)
    # 23:30-23:55 is the held half hour, which carries no energy of its own and so is taken at the car's 7.4 kW
    expected = house + 7.4 / 60.0 * (my_predbat.minutes_now - 1410) * (RATE_PEAK - RATE_OFF_PEAK)
    failed |= _check("t4 cost at 23:55 without a car sensor", abs(cost - expected) < 0.5, "got {:.2f}p expected {:.2f}p".format(cost, expected))

    return failed


def test_iog_cap_cost_today(my_predbat):
    """
    Car charging beyond the Octopus Intelligent cap is in today's cost only as far as it has happened.
    """
    print("*** Running test: IOG cap cost today")
    fields = STATE_FIELDS + EXTRA_FIELDS
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in fields}
    saved_args = {key: my_predbat.args.get(key) for key in ("octopus_slot_low_rate", "octopus_slot_max", "octopus_intelligent_slot")}
    try:
        failed = _run(my_predbat)
    finally:
        for key, value in saved_args.items():
            if value is None:
                my_predbat.args.pop(key, None)
            else:
                my_predbat.args[key] = value
        for field, value in saved_state.items():
            setattr(my_predbat, field, value)
    print("*** IOG cap cost today test {}".format("FAILED" if failed else "PASSED"))
    return failed
