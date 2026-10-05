# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the Intelligent Octopus car-need gate (#4482).

With octopus_intelligent_consider_full on, load_octopus_slots() ends a car's slots where it reaches its
limit. A future dispatch minute no car still needs is one the car won't draw in, and Octopus only bills a
dispatch cheap when the car charges, so its cheap rate is withheld from the house battery's plan - both
the discount rate_add_io_slots() would add and one the rate feed already delivered.
"""

import copy
from datetime import timedelta

from tests.test_infra import reset_rates

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_ATTRS = [
    "minutes_now",
    "forecast_minutes",
    "octopus_intelligent_consider_full",
    "octopus_slots",
    "car_charging_slots",
    "car_charging_soc",
    "car_charging_limit",
    "car_charging_loss",
    "car_charging_rate",
    "num_cars",
    "io_adjusted",
    "octopus_surplus",
    "dynamic_load_car_effective",
    "dynamic_load_car_stripped",
    "rate_min",
    "rate_min_base",
    "rate_max_base",
    "rate_import",
    "rate_export",
]


def _slot(my_predbat, start_hour, end_hour, kwh):
    """A raw Octopus dispatch from start_hour to end_hour today, as the integration reports it."""
    start = my_predbat.midnight_utc + timedelta(hours=start_hour)
    end = my_predbat.midnight_utc + timedelta(hours=end_hour)
    return {"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "charge_in_kwh": kwh, "source": "smart-charge", "location": "AT_HOME"}


def _day_rates(minute_from, minute_to, rate):
    """A flat rate table."""
    return {minute: rate for minute in range(minute_from, minute_to)}


def _expect(name, condition, detail):
    """Print and return True on failure."""
    if not condition:
        print("  ERROR: {}: {}".format(name, detail))
        return True
    return False


def run_surplus_minutes_tests(my_predbat):
    """octopus_surplus_minutes() on hand-built capped slots."""
    failed = False
    my_predbat.minutes_now = 10 * 60
    my_predbat.num_cars = 1
    my_predbat.octopus_slots = [[_slot(my_predbat, 13, 15, 20.0)]]
    needed = [{"start": 13 * 60, "end": 13 * 60 + 20, "kwh": 5.0, "octopus": True}, {"start": 13 * 60 + 20, "end": 15 * 60, "kwh": 0.0, "octopus": True}]
    my_predbat.car_charging_slots = [needed]

    my_predbat.octopus_intelligent_consider_full = False
    failed |= _expect("consider_full off", my_predbat.octopus_surplus_minutes() == set(), "the gate must be off")

    my_predbat.octopus_intelligent_consider_full = True
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("partial need rounds out", surplus == set(range(13 * 60 + 30, 15 * 60)), "expected 13:30-15:00, the rest of the half hour the car needs stays cheap, got {}-{}".format(min(surplus or [0]), max(surplus or [0])))

    # Fixed 23:30-05:30 window is cheap by tariff, never touched
    my_predbat.octopus_slots = [[_slot(my_predbat, 23, 24.5, 20.0)]]
    my_predbat.car_charging_slots = [[{"start": 23 * 60, "end": 24 * 60 + 30, "kwh": 0.0, "octopus": True}]]
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("fixed window untouched", surplus == set(range(23 * 60, 23 * 60 + 30)), "only 23:00-23:30 is dispatch-only, got {}".format(sorted(surplus)[:1] + sorted(surplus)[-1:]))

    # Elapsed minutes are history - only from now onwards
    my_predbat.octopus_slots = [[_slot(my_predbat, 9, 11, 20.0)]]
    my_predbat.car_charging_slots = [[{"start": 9 * 60, "end": 11 * 60, "kwh": 0.0, "octopus": True}]]
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("elapsed untouched", surplus == set(range(10 * 60, 11 * 60)), "expected 10:00-11:00 only")

    # Rates are shared: a minute another car still needs stays cheap
    my_predbat.num_cars = 2
    my_predbat.octopus_slots = [[_slot(my_predbat, 13, 15, 20.0)], [_slot(my_predbat, 14, 14.5, 5.0)]]
    my_predbat.car_charging_slots = [needed, [{"start": 14 * 60, "end": 14 * 60 + 30, "kwh": 5.0, "octopus": True}]]
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("other car's need kept", not (surplus & set(range(14 * 60, 14 * 60 + 30))) and (13 * 60 + 30) in surplus, "14:00-14:30 is needed by car 1")

    # A cancelled car's slots are zeroed with the kWh kept in kwh_cancelled: still needed, cancellation
    # (#5229) decides their rate - including the rest of a half hour the car was seen charging in
    cancelled = [{"start": 13 * 60, "end": 13 * 60 + 20, "kwh": 0, "kwh_cancelled": 5.0, "octopus": True}, {"start": 13 * 60 + 20, "end": 15 * 60, "kwh": 0, "octopus": True}]
    my_predbat.num_cars = 1
    my_predbat.octopus_slots = [[_slot(my_predbat, 13, 15, 20.0)]]
    my_predbat.car_charging_slots = [cancelled]
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("cancelled car's need kept", surplus == set(range(13 * 60 + 30, 15 * 60)), "13:00-13:30 is still needed by the cancelled car")

    # A car not modelled from its Octopus slots (e.g. ignored while unplugged) is left alone
    my_predbat.num_cars = 1
    my_predbat.octopus_slots = [[_slot(my_predbat, 13, 15, 20.0)]]
    my_predbat.car_charging_slots = [[]]
    failed |= _expect("car not modelled is ignored", my_predbat.octopus_surplus_minutes() == set(), "no capped slots to compare with")
    return failed


def _end_to_end(my_predbat, consider_full, feed):
    """
    Car at 95 of 100 kWh with a 13:00-15:00 dispatch Octopus sized at 20 kWh: the car needs 5 kWh, the
    first half hour. Runs the real capping, surplus and both rate paths, returns (rates, io_adjusted).
    With feed, the rate feed has already discounted and flagged the whole dispatch.
    """
    my_predbat.minutes_now = 10 * 60
    my_predbat.forecast_minutes = 3 * 24 * 60
    my_predbat.num_cars = 1
    my_predbat.args["octopus_slot_low_rate"] = True
    my_predbat.args["octopus_slot_max"] = 12
    reset_rates(my_predbat, 30, 5)
    my_predbat.rate_min = 4
    my_predbat.rate_min_base = 4
    my_predbat.rate_max_base = 30
    my_predbat.car_charging_soc = [95.0]
    my_predbat.car_charging_limit = [100.0]
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_rate = [10.0]
    my_predbat.dynamic_load_car_effective = {}
    my_predbat.octopus_intelligent_consider_full = consider_full
    my_predbat.octopus_slots = [[_slot(my_predbat, 13, 15, 20.0)]]
    my_predbat.car_charging_slots = [my_predbat.load_octopus_slots(0, my_predbat.octopus_slots[0], consider_full)]

    rates = _day_rates(-96 * 60, 3 * 24 * 60, 30.0)
    my_predbat.io_adjusted = {}
    if feed:
        for minute in range(13 * 60, 15 * 60):
            rates[minute] = 4.0
            my_predbat.io_adjusted[minute] = True
    my_predbat.octopus_surplus = my_predbat.octopus_surplus_minutes()
    rates = my_predbat.dynamic_load_car_strip_feed_rates(rates)
    rates = my_predbat.rate_add_io_slots(0, rates, my_predbat.octopus_slots[0])
    return rates, my_predbat.io_adjusted, my_predbat.car_charging_slots[0]


def run_end_to_end_tests(my_predbat):
    """The capping, the surplus and both rate paths together."""
    failed = False
    needed = range(13 * 60, 13 * 60 + 30)
    surplus = range(13 * 60 + 30, 15 * 60)

    for feed in (False, True):
        path = "feed" if feed else "overlay"
        rates, io_adjusted, car_slots = _end_to_end(my_predbat, True, feed)
        print("  {} path, capped car slots {}".format(path, [(slot["start"], slot["end"], slot["kwh"]) for slot in car_slots]))
        failed |= _expect("{}: needed half hour cheap".format(path), all(rates[minute] == 4.0 for minute in needed), "13:00-13:30 should be 4p")
        failed |= _expect("{}: needed half hour flagged".format(path), all(io_adjusted.get(minute, False) for minute in needed), "13:00-13:30 should stay io_adjusted")
        failed |= _expect("{}: surplus at peak".format(path), all(rates[minute] == 30.0 for minute in surplus), "13:30-15:00 should be 30p, got {}".format(sorted({rates[minute] for minute in surplus})))
        failed |= _expect("{}: surplus unflagged".format(path), not any(io_adjusted.get(minute, False) for minute in surplus), "13:30-15:00 should not be io_adjusted")

    rates, io_adjusted, _ = _end_to_end(my_predbat, False, False)
    failed |= _expect("consider_full off: whole dispatch cheap", all(rates[minute] == 4.0 for minute in range(13 * 60, 15 * 60)), "behaviour must be unchanged with the switch off")
    return failed


def run_iog_car_need_gate_tests(my_predbat):
    """Run the car-need gate tests, returns True on failure."""
    print("**** Running IOG car-need gate tests ****")
    saved = {attr: copy.deepcopy(getattr(my_predbat, attr)) for attr in _ATTRS if hasattr(my_predbat, attr)}
    saved_args = {key: my_predbat.args[key] for key in ("octopus_slot_low_rate", "octopus_slot_max") if key in my_predbat.args}
    failed = False
    try:
        failed |= run_surplus_minutes_tests(my_predbat)
        failed |= run_end_to_end_tests(my_predbat)
    finally:
        for attr, value in saved.items():
            setattr(my_predbat, attr, value)
        for key in ("octopus_slot_low_rate", "octopus_slot_max"):
            if key in saved_args:
                my_predbat.args[key] = saved_args[key]
            else:
                my_predbat.args.pop(key, None)
    if failed:
        print("**** IOG car-need gate tests FAILED ****")
    else:
        print("**** IOG car-need gate tests passed ****")
    return failed
