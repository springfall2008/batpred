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
Holding the half hours an Octopus Intelligent planned dispatch has run in (#5520).

A planned dispatch only covers what is still to come, and Octopus lists what has run as a completed
dispatch 15 to 60 minutes later. In between the half hour used to go back to the day rate, so today's
cost stepped up after each half hour of car charging and fell back later. A half hour seen running, with
the car not cancelled by slot confirmation (octopus_intelligent_dynamic), now keeps the dispatch rate
until a completed dispatch covers it or OCTOPUS_DISPATCH_HOLD_MINUTES have passed.
"""
import copy
from datetime import timedelta

from const import OCTOPUS_DISPATCH_HOLD_MINUTES

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
RATE_LOW = 6.57
RATE_DAY = 27.7

STATE_FIELDS = (
    "num_cars",
    "minutes_now",
    "now_utc",
    "now_utc_real",
    "midnight_utc",
    "forecast_minutes",
    "rate_min_base",
    "rate_max_base",
    "rate_import",
    "io_adjusted",
    "octopus_slots",
    "octopus_slots_planned",
    "octopus_slots_held",
    "octopus_surplus",
    "octopus_intelligent_dynamic",
    "octopus_intelligent_trust_slots",
    "car_charging_slots",
    "car_charging_rate",
    "car_charging_soc",
    "car_charging_limit",
    "car_charging_loss",
    "dynamic_load_car_effective",
    "dynamic_load_car_confirmed",
)


def _check(name, condition, detail=""):
    """
    Print the outcome of one check, returning True when it failed.
    """
    if condition:
        return False
    print("ERROR: {} {}".format(name, detail))
    return True


def _dispatch(my_predbat, start_minute, end_minute, kwh=2.0, source="smart-charge", location="AT_HOME"):
    """
    A raw Octopus dispatch entry covering start_minute to end_minute past midnight.
    """
    start = my_predbat.midnight_utc + timedelta(minutes=start_minute)
    end = my_predbat.midnight_utc + timedelta(minutes=end_minute)
    return {"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "charge_in_kwh": kwh, "source": source, "location": location}


def _reset(my_predbat):
    """
    One Octopus Intelligent car on a 6.57p / 27.7p tariff, with nothing held.
    """
    my_predbat.num_cars = 1
    my_predbat.forecast_minutes = 2 * 24 * 60
    my_predbat.rate_min_base = RATE_LOW
    my_predbat.rate_max_base = RATE_DAY
    my_predbat.rate_import = {}
    my_predbat.io_adjusted = {}
    my_predbat.octopus_slots = [[]]
    my_predbat.octopus_slots_planned = {}
    my_predbat.octopus_slots_held = {}
    my_predbat.octopus_surplus = set()
    my_predbat.octopus_intelligent_dynamic = True
    my_predbat.octopus_intelligent_trust_slots = True
    my_predbat.car_charging_slots = [[]]
    my_predbat.car_charging_rate = [7.4]
    my_predbat.car_charging_soc = [10.0]
    my_predbat.car_charging_limit = [100.0]
    my_predbat.car_charging_loss = 1.0
    my_predbat.dynamic_load_car_effective = {}
    my_predbat.dynamic_load_car_confirmed = {}


def _cycle(my_predbat, minute, planned=None, completed=None, cancelled=False, save=True, seconds=0):
    """
    One fetch cycle at minute past midnight, in the order fetch_sensor_data() works: merge the completed,
    held and planned dispatches, build the car's slots, apply this cycle's slot confirmation decision
    (cancelled), record the half hour in progress and build the rates. Returns the rates.
    """
    planned = planned or []
    completed = completed or []
    my_predbat.now_utc_real = my_predbat.midnight_utc + timedelta(minutes=minute, seconds=seconds)
    my_predbat.minutes_now = int(minute / 5) * 5
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=my_predbat.minutes_now)

    slots = list(completed)
    slots += my_predbat.octopus_held_dispatches(0, completed, planned)
    slots += planned
    my_predbat.octopus_slots = [slots]
    my_predbat.octopus_slots_planned = {0: planned}
    my_predbat.car_charging_slots = [my_predbat.load_octopus_slots(0, slots, False)]
    my_predbat.dynamic_load_car_effective = {0: cancelled}
    if save:
        my_predbat.octopus_held_update()

    my_predbat.io_adjusted = {}
    rates = {minute: RATE_DAY for minute in range(-2 * 24 * 60, 2 * 24 * 60)}
    return my_predbat.rate_add_io_slots(0, rates, slots)


def _low(rates, start_minute, end_minute):
    """
    Whether every minute from start_minute up to end_minute has the dispatch rate.
    """
    return all(rates[minute] == RATE_LOW for minute in range(start_minute, end_minute))


def _day(rates, start_minute, end_minute):
    """
    Whether every minute from start_minute up to end_minute has the day rate.
    """
    return all(rates[minute] == RATE_DAY for minute in range(start_minute, end_minute))


def _held(my_predbat):
    """
    The held dispatches in car 0's slots this cycle.
    """
    return [slot for slot in my_predbat.octopus_slots[0] if slot.get("held", False)]


def _run(my_predbat):
    """
    Run the scenarios, returning True when any check failed.
    """
    failed = False
    hold = OCTOPUS_DISPATCH_HOLD_MINUTES

    print("Test 1: a half hour of a running dispatch keeps its rate once it has passed (the #5520 report)")
    _reset(my_predbat)
    # The Octopus API component advances the start of the dispatch to its last poll, here 2 minutes before each cycle
    rates = _cycle(my_predbat, 1075, planned=[_dispatch(my_predbat, 1073, 1800, kwh=70.0, location=None)])
    failed |= _check("t1 half hour in progress is cheap", _low(rates, 1050, 1080))
    failed |= _check("t1 nothing held while the planned dispatch covers it", _held(my_predbat) == [], "held {}".format(_held(my_predbat)))
    rates = _cycle(my_predbat, 1085, planned=[_dispatch(my_predbat, 1083, 1800, kwh=69.0, location=None)])
    failed |= _check("t1 17:30-18:00 still cheap at 18:05", _low(rates, 1050, 1080), "rate {}".format(rates[1050]))
    failed |= _check("t1 17:00-17:30 never seen running, day rate", _day(rates, 1020, 1050), "rate {}".format(rates[1020]))
    failed |= _check("t1 one held half hour", len(_held(my_predbat)) == 1, "held {}".format(_held(my_predbat)))
    failed |= _check("t1 held half hour plans no car charging", all(slot["start"] >= 1083 for slot in my_predbat.car_charging_slots[0]), "slots {}".format(my_predbat.car_charging_slots[0]))
    rates = _cycle(my_predbat, 1115, planned=[_dispatch(my_predbat, 1113, 1800, kwh=66.0, location=None)])
    failed |= _check("t1 both elapsed half hours cheap at 18:35", _low(rates, 1050, 1110), "rates {} {}".format(rates[1050], rates[1080]))

    print("Test 2: the completed dispatch takes over when Octopus lists it")
    completed = [_dispatch(my_predbat, 1050, 1080, kwh=3.8)]
    rates = _cycle(my_predbat, 1125, planned=[_dispatch(my_predbat, 1123, 1800, kwh=65.0, location=None)], completed=completed)
    failed |= _check("t2 still cheap", _low(rates, 1050, 1110))
    failed |= _check("t2 only the half hour not yet listed is held", [slot["start"] for slot in _held(my_predbat)] == [_dispatch(my_predbat, 1080, 1110)["start"]], "held {}".format(_held(my_predbat)))
    # A completed dispatch of no energy is treated as never having happened, and is the record once listed
    completed.append(_dispatch(my_predbat, 1080, 1110, kwh=0.0))
    rates = _cycle(my_predbat, 1130, planned=[_dispatch(my_predbat, 1128, 1800, kwh=64.0, location=None)], completed=completed)
    failed |= _check("t2 listed with no energy goes back to the day rate", _day(rates, 1080, 1110) and _low(rates, 1050, 1080), "rate {}".format(rates[1080]))

    print("Test 3: a half hour Octopus never lists goes back to the day rate when the hold runs out")
    _reset(my_predbat)
    _cycle(my_predbat, 1075, planned=[_dispatch(my_predbat, 1050, 1080)])
    rates = _cycle(my_predbat, 1080 + hold - 5)
    failed |= _check("t3 held to the end of the hold", _low(rates, 1050, 1080), "rate {}".format(rates[1050]))
    rates = _cycle(my_predbat, 1080 + hold)
    failed |= _check("t3 day rate after the hold", _day(rates, 1050, 1080), "rate {}".format(rates[1050]))
    failed |= _check("t3 record dropped", my_predbat.octopus_slots_held.get(0) == {}, "held {}".format(my_predbat.octopus_slots_held))

    print("Test 4: a half hour the car was cancelled in is not held")
    _reset(my_predbat)
    _cycle(my_predbat, 1055, planned=[_dispatch(my_predbat, 1053, 1200)])
    _cycle(my_predbat, 1085, planned=[_dispatch(my_predbat, 1083, 1200)])
    _cycle(my_predbat, 1095, planned=[_dispatch(my_predbat, 1093, 1200)], cancelled=True)
    rates = _cycle(my_predbat, 1115, cancelled=False)
    failed |= _check("t4 cancelled half hour at the day rate", _day(rates, 1080, 1110), "rate {}".format(rates[1080]))
    failed |= _check("t4 earlier half hour still held", _low(rates, 1050, 1080), "rate {}".format(rates[1050]))

    print("Test 5: cancelled after charging in the half hour, which Octopus bills whole, is held")
    _reset(my_predbat)
    _cycle(my_predbat, 1085, planned=[_dispatch(my_predbat, 1083, 1200)])
    my_predbat.dynamic_load_car_confirmed = {0: my_predbat.midnight_utc + timedelta(minutes=1110)}
    _cycle(my_predbat, 1095, planned=[_dispatch(my_predbat, 1093, 1200)], cancelled=True)
    rates = _cycle(my_predbat, 1115)
    failed |= _check("t5 confirmed half hour held", _low(rates, 1080, 1110), "rate {}".format(rates[1080]))

    print("Test 6: a dispatch that ends part-way through a half hour keeps the whole of it")
    _reset(my_predbat)
    _cycle(my_predbat, 1125, planned=[_dispatch(my_predbat, 1123, 1132)])
    rates = _cycle(my_predbat, 1135)
    failed |= _check("t6 whole half hour cheap after the dispatch has gone", _low(rates, 1110, 1140), "rates {} {}".format(rates[1110], rates[1139]))
    failed |= _check("t6 next half hour untouched", _day(rates, 1140, 1170))
    failed |= _check("t6 no car charging planned", my_predbat.car_charging_slots[0] == [], "slots {}".format(my_predbat.car_charging_slots[0]))
    signature = my_predbat.octopus_slots_signature(my_predbat.octopus_slots)
    rates = _cycle(my_predbat, 1138)
    failed |= _check("t6 held slot moving with now is not a slot change", signature == my_predbat.octopus_slots_signature(my_predbat.octopus_slots))
    rates = _cycle(my_predbat, 1145)
    failed |= _check("t6 still cheap once the half hour has ended", _low(rates, 1110, 1140) and _day(rates, 1140, 1170), "rate {}".format(rates[1110]))

    print("Test 7: a dispatch still to start is not held, as Octopus may withdraw it")
    _reset(my_predbat)
    rates = _cycle(my_predbat, 1085, planned=[_dispatch(my_predbat, 1100, 1200)])
    failed |= _check("t7 priced while planned", _low(rates, 1080, 1110))
    rates = _cycle(my_predbat, 1090)
    failed |= _check("t7 day rate once withdrawn", _day(rates, 1080, 1110), "rate {}".format(rates[1080]))
    failed |= _check("t7 nothing recorded", not my_predbat.octopus_slots_held.get(0), "held {}".format(my_predbat.octopus_slots_held))

    print("Test 8: a dispatch that is not billed off-peak is not held")
    _reset(my_predbat)
    _cycle(my_predbat, 1085, planned=[_dispatch(my_predbat, 1083, 1200, source="bump-charge")])
    _cycle(my_predbat, 1085, planned=[_dispatch(my_predbat, 1083, 1200, location="AWAY")])
    failed |= _check("t8 nothing recorded", not my_predbat.octopus_slots_held.get(0), "held {}".format(my_predbat.octopus_slots_held))

    print("Test 9: a compare run (save False) records nothing")
    _reset(my_predbat)
    _cycle(my_predbat, 1085, planned=[_dispatch(my_predbat, 1083, 1200)], save=False)
    failed |= _check("t9 nothing recorded", not my_predbat.octopus_slots_held.get(0), "held {}".format(my_predbat.octopus_slots_held))

    print("Test 10: a held half hour survives midnight")
    _reset(my_predbat)
    _cycle(my_predbat, 1435, planned=[_dispatch(my_predbat, 1433, 1440)])
    my_predbat.midnight_utc = my_predbat.midnight_utc + timedelta(days=1)
    rates = _cycle(my_predbat, 10)
    failed |= _check("t10 23:30-00:00 yesterday still cheap", _low(rates, -30, 0), "rate {}".format(rates[-30]))
    failed |= _check("t10 today untouched", _day(rates, 0, 30), "rate {}".format(rates[0]))

    print("Test 11: with slot confirmation off nothing is cancelled, so the half hour is held")
    _reset(my_predbat)
    my_predbat.octopus_intelligent_dynamic = False
    _cycle(my_predbat, 1085, planned=[_dispatch(my_predbat, 1083, 1200)])
    rates = _cycle(my_predbat, 1115)
    failed |= _check("t11 held", _low(rates, 1080, 1110), "rate {}".format(rates[1080]))

    print("Test 12: a planned entry first seen after it has ended was never seen running, so is not held")
    _reset(my_predbat)
    # Still listed as planned 3 minutes after its end, and by then priced as finished whatever its location
    _cycle(my_predbat, 1095, planned=[_dispatch(my_predbat, 1083, 1092, location="AWAY")])
    failed |= _check("t12 away entry not recorded", not my_predbat.octopus_slots_held.get(0), "held {}".format(my_predbat.octopus_slots_held))
    _cycle(my_predbat, 1097, planned=[_dispatch(my_predbat, 1083, 1096)], seconds=30)
    failed |= _check("t12 entry ended since minutes_now not recorded", not my_predbat.octopus_slots_held.get(0), "held {}".format(my_predbat.octopus_slots_held))
    rates = _cycle(my_predbat, 1100)
    failed |= _check("t12 day rate once the entry has gone", _day(rates, 1080, 1110), "rate {}".format(rates[1080]))

    return failed


def test_octopus_dispatch_hold(my_predbat):
    """
    The half hours a planned Octopus Intelligent dispatch has run in keep the dispatch rate until Octopus
    lists them as completed.
    """
    print("*** Running test: Octopus dispatch hold")
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in STATE_FIELDS}
    saved_args = {key: my_predbat.args.get(key) for key in ("octopus_slot_low_rate", "octopus_slot_max")}
    try:
        my_predbat.args["octopus_slot_low_rate"] = True
        my_predbat.args["octopus_slot_max"] = 48
        failed = _run(my_predbat)
    finally:
        for key, value in saved_args.items():
            if value is None:
                my_predbat.args.pop(key, None)
            else:
                my_predbat.args[key] = value
        for field, value in saved_state.items():
            setattr(my_predbat, field, value)
    print("*** Octopus dispatch hold test {}".format("FAILED" if failed else "PASSED"))
    return failed
