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
    "dynamic_load_car_confirmed",
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
    my_predbat.dynamic_load_car_effective = {}
    my_predbat.dynamic_load_car_confirmed = {}
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

    # A dispatch in progress with the car already full: from the next half hour it is surplus like any other
    # (its capped slots are worked out from now, see load_octopus_slots()). Elapsed minutes are never touched.
    my_predbat.octopus_slots = [[_slot(my_predbat, 9, 11, 20.0)]]
    my_predbat.car_charging_slots = [[{"start": 10 * 60, "end": 11 * 60, "kwh": 0.0, "octopus": True}]]
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("in-progress dispatch from the next half hour", surplus == set(range(10 * 60 + 30, 11 * 60)), "expected 10:30-11:00, got {}".format(sorted(surplus)[:1] + sorted(surplus)[-1:]))

    # The clock is floored to five minutes, so a fetch at 14:02 reads 14:00: the current half hour must still be
    # kept, as the car may already have charged in it. A dispatch starting later in it rounds out to 14:00.
    my_predbat.minutes_now = 14 * 60
    my_predbat.octopus_slots = [[_slot(my_predbat, 14.25, 15, 20.0)]]
    my_predbat.car_charging_slots = [[{"start": 14 * 60 + 15, "end": 15 * 60, "kwh": 0.0, "octopus": True}]]
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("current half hour kept on the boundary", surplus == set(range(14 * 60 + 30, 15 * 60)), "expected 14:30-15:00 only, got {}".format(sorted(surplus)[:1] + sorted(surplus)[-1:]))

    # And mid half hour
    my_predbat.minutes_now = 14 * 60 + 10
    my_predbat.octopus_slots = [[_slot(my_predbat, 14 + 20 / 60, 15, 20.0)]]
    my_predbat.car_charging_slots = [[{"start": 14 * 60 + 20, "end": 15 * 60, "kwh": 0.0, "octopus": True}]]
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("current half hour kept mid-period", surplus == set(range(14 * 60 + 30, 15 * 60)), "expected 14:30-15:00 only, got {}".format(sorted(surplus)[:1] + sorted(surplus)[-1:]))
    my_predbat.minutes_now = 10 * 60

    # Rates are shared: a minute another car still needs stays cheap
    my_predbat.num_cars = 2
    my_predbat.octopus_slots = [[_slot(my_predbat, 13, 15, 20.0)], [_slot(my_predbat, 14, 14.5, 5.0)]]
    my_predbat.car_charging_slots = [needed, [{"start": 14 * 60, "end": 14 * 60 + 30, "kwh": 5.0, "octopus": True}]]
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("other car's need kept", not (surplus & set(range(14 * 60, 14 * 60 + 30))) and (13 * 60 + 30) in surplus, "14:00-14:30 is needed by car 1")

    # A cancelled car's need counts only until its cheap rate ends (strip_from): its slots are zeroed with the kWh
    # kept in kwh_cancelled, and it mustn't keep a minute cheap for a full car that won't charge in it either
    my_predbat.dynamic_load_car_effective = {0: False, 1: True}
    my_predbat.octopus_slots = [[_slot(my_predbat, 13, 15, 20.0)], [_slot(my_predbat, 14, 15, 10.0)]]
    my_predbat.car_charging_slots = [needed, [{"start": 14 * 60, "end": 15 * 60, "kwh": 0, "kwh_cancelled": 10.0, "octopus": True}]]
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("cancelled car's need not shared", surplus == set(range(13 * 60 + 30, 15 * 60)), "car 1 is cancelled, so 14:00-15:00 is nobody's, got {}".format(sorted(surplus)[:1] + sorted(surplus)[-1:]))

    # ...but the rest of a half hour a cancelled car was seen charging in stays cheap (GH#5316), and no more
    my_predbat.num_cars = 1
    my_predbat.minutes_now = 13 * 60 + 10
    my_predbat.dynamic_load_car_effective = {0: True}
    my_predbat.dynamic_load_car_confirmed = {0: my_predbat.midnight_utc + timedelta(hours=13, minutes=30)}
    my_predbat.octopus_slots = [[_slot(my_predbat, 13, 15, 20.0)]]
    my_predbat.car_charging_slots = [[{"start": 13 * 60, "end": 13 * 60 + 20, "kwh": 0, "kwh_cancelled": 5.0, "octopus": True}, {"start": 13 * 60 + 20, "end": 15 * 60, "kwh": 0, "octopus": True}]]
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("cancelled car kept only to strip_from", surplus == set(range(13 * 60 + 30, 15 * 60)), "expected 13:30-15:00, got {}".format(sorted(surplus)[:1] + sorted(surplus)[-1:]))
    my_predbat.minutes_now = 10 * 60
    my_predbat.dynamic_load_car_effective = {}
    my_predbat.dynamic_load_car_confirmed = {}

    # A car not modelled from its Octopus slots (e.g. ignored while unplugged) keeps its dispatches cheap...
    my_predbat.octopus_slots = [[_slot(my_predbat, 13, 15, 20.0)]]
    my_predbat.car_charging_slots = [[]]
    failed |= _expect("car not modelled is kept", my_predbat.octopus_surplus_minutes() == set(), "no capped slots to compare with")

    # ...including where a full car's dispatch overlaps it, unless that car is cancelled
    my_predbat.num_cars = 2
    my_predbat.octopus_slots = [[_slot(my_predbat, 13, 15, 20.0)], [_slot(my_predbat, 14, 15, 10.0)]]
    my_predbat.car_charging_slots = [needed, []]
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("dispatch of a car not modelled kept", surplus == set(range(13 * 60 + 30, 14 * 60)), "expected 13:30-14:00, got {}".format(sorted(surplus)[:1] + sorted(surplus)[-1:]))
    my_predbat.dynamic_load_car_effective = {1: True}
    surplus = my_predbat.octopus_surplus_minutes()
    failed |= _expect("cancelled car not modelled is not kept", surplus == set(range(13 * 60 + 30, 15 * 60)), "expected 13:30-15:00, got {}".format(sorted(surplus)[:1] + sorted(surplus)[-1:]))
    my_predbat.dynamic_load_car_effective = {}
    my_predbat.num_cars = 1
    return failed


def _end_to_end(my_predbat, consider_full, feed, minutes_now=10 * 60, cancelled_second_car=False):
    """
    Car at 95 of 100 kWh with a 13:00-15:00 dispatch Octopus sized at 20 kWh: the car needs 5 kWh, the
    first half hour. Runs the real capping, surplus and both rate paths, returns (rates, io_adjusted).
    With feed, the rate feed has already discounted and flagged the whole dispatch. With
    cancelled_second_car, a second car whose 14:00-15:00 dispatch dynamic load has cancelled shares it.
    The dispatch is left untrimmed, as octopus_intelligent_slot reports it.
    """
    my_predbat.minutes_now = minutes_now
    my_predbat.forecast_minutes = 3 * 24 * 60
    my_predbat.num_cars = 2 if cancelled_second_car else 1
    my_predbat.args["octopus_slot_low_rate"] = True
    my_predbat.args["octopus_slot_max"] = 12
    reset_rates(my_predbat, 30, 5)
    my_predbat.rate_min = 4
    my_predbat.rate_min_base = 4
    my_predbat.rate_max_base = 30
    my_predbat.car_charging_soc = [95.0, 0.0]
    my_predbat.car_charging_limit = [100.0, 100.0]
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_rate = [10.0, 10.0]
    my_predbat.dynamic_load_car_effective = {}
    my_predbat.dynamic_load_car_confirmed = {}
    my_predbat.octopus_intelligent_consider_full = consider_full
    my_predbat.octopus_slots = [[_slot(my_predbat, 13, 15, 20.0)]]
    my_predbat.car_charging_slots = [my_predbat.load_octopus_slots(0, my_predbat.octopus_slots[0], consider_full)]
    if cancelled_second_car:
        # As dynamic_load_car_check() leaves a cancelled car: future slots zeroed, the kWh kept
        my_predbat.octopus_slots.append([_slot(my_predbat, 14, 15, 10.0)])
        my_predbat.car_charging_slots.append([{"start": 14 * 60, "end": 15 * 60, "kwh": 0, "kwh_cancelled": 10.0, "octopus": True}])
        my_predbat.dynamic_load_car_effective = {0: False, 1: True}

    rates = _day_rates(-96 * 60, 3 * 24 * 60, 30.0)
    my_predbat.io_adjusted = {}
    if feed:
        for minute in range(13 * 60, 15 * 60):
            rates[minute] = 4.0
            my_predbat.io_adjusted[minute] = True
    my_predbat.octopus_surplus = my_predbat.octopus_surplus_minutes()
    rates = my_predbat.dynamic_load_car_strip_feed_rates(rates)
    for car_n in range(my_predbat.num_cars):
        rates = my_predbat.rate_add_io_slots(car_n, rates, my_predbat.octopus_slots[car_n])
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

    for feed in (False, True):
        path = "feed" if feed else "overlay"
        # In progress at 14:10, untrimmed as octopus_intelligent_slot reports it: 5 kWh at the remaining 10 kW
        # runs to 14:40, so the car's need is capped from now and 14:30-15:00 stays cheap
        rates, io_adjusted, _ = _end_to_end(my_predbat, True, feed, minutes_now=14 * 60 + 10)
        failed |= _expect(
            "{}: in-progress dispatch capped from now".format(path), all(rates[minute] == 4.0 for minute in range(14 * 60 + 10, 15 * 60)), "14:10-15:00 should stay 4p, got {}".format(sorted({rates[minute] for minute in range(14 * 60 + 10, 15 * 60)}))
        )

        # A cancelled second car's need mustn't keep the full car's surplus cheap: neither will charge then
        rates, io_adjusted, _ = _end_to_end(my_predbat, True, feed, cancelled_second_car=True)
        failed |= _expect("{}: needed half hour still cheap with a cancelled car".format(path), all(rates[minute] == 4.0 for minute in needed), "13:00-13:30 should be 4p")
        failed |= _expect("{}: cancelled car's need not shared".format(path), all(rates[minute] == 30.0 for minute in surplus), "13:30-15:00 should be 30p, got {}".format(sorted({rates[minute] for minute in surplus})))
    return failed


def _long_dispatch_surplus(my_predbat, minutes_now, chunked):
    """
    #5403 review: car at 55 of 60 kWh (needs 5 kWh), consider_full on, Octopus charging 13:00-17:00 either as
    one 28 kWh dispatch or as eight half-hour 3.5 kWh dispatches. Returns the surplus from the real capping.
    """
    my_predbat.minutes_now = minutes_now
    my_predbat.forecast_minutes = 3 * 24 * 60
    my_predbat.num_cars = 1
    my_predbat.args["octopus_slot_low_rate"] = True
    my_predbat.args["octopus_slot_max"] = 12
    reset_rates(my_predbat, 30, 5)
    my_predbat.rate_min = 4
    my_predbat.rate_min_base = 4
    my_predbat.rate_max_base = 30
    my_predbat.car_charging_soc = [55.0]
    my_predbat.car_charging_limit = [60.0]
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_rate = [7.0]
    my_predbat.dynamic_load_car_effective = {}
    my_predbat.dynamic_load_car_confirmed = {}
    my_predbat.octopus_intelligent_consider_full = True
    if chunked:
        dispatches = [_slot(my_predbat, 13 + n / 2, 13.5 + n / 2, 3.5) for n in range(8)]
    else:
        dispatches = [_slot(my_predbat, 13, 17, 28.0)]
    my_predbat.octopus_slots = [dispatches]
    my_predbat.car_charging_slots = [my_predbat.load_octopus_slots(0, dispatches, True)]
    return my_predbat.octopus_surplus_minutes()


def run_long_dispatch_tests(my_predbat):
    """The gate must give the same answer whether or not a long dispatch has started, however Octopus chunks it."""
    failed = False
    for minutes_now in (12 * 60 + 55, 13 * 60 + 5):
        for chunked in (False, True):
            surplus = _long_dispatch_surplus(my_predbat, minutes_now, chunked)
            name = "{} at {:02d}:{:02d}".format("eight half hours" if chunked else "one dispatch", minutes_now // 60, minutes_now % 60)
            failed |= _expect(name, surplus == set(range(14 * 60, 17 * 60)), "expected 14:00-17:00 withheld, got {}".format(sorted(surplus)[:1] + sorted(surplus)[-1:]))
    return failed


def run_replan_trigger_tests(my_predbat):
    """A surplus change forces a replan, but time passing alone does not."""
    failed = False
    my_predbat.minutes_now = 13 * 60 + 5
    my_predbat.octopus_surplus = set(range(14 * 60, 17 * 60))
    failed |= _expect("unchanged", not my_predbat.octopus_surplus_changed(set(range(14 * 60, 17 * 60))), "same surplus is not a change")
    failed |= _expect("car needs more", my_predbat.octopus_surplus_changed(set(range(15 * 60, 17 * 60))), "a different future surplus is a change")
    # The cycle at 12:55 withheld 13:00-17:00; at 13:05 the current half hour is no longer withheld
    my_predbat.octopus_surplus = set(range(13 * 60 + 30, 17 * 60))
    failed |= _expect("time passing", not my_predbat.octopus_surplus_changed(set(range(13 * 60, 17 * 60))), "a previous surplus that now starts in the current half hour is not a change")
    failed |= _expect("gate switched on", my_predbat.octopus_surplus_changed(set()), "a new surplus is a change")
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
        failed |= run_long_dispatch_tests(my_predbat)
        failed |= run_replan_trigger_tests(my_predbat)
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
