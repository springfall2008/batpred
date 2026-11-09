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

from const import DYNAMIC_LOAD_CAR_START_MINUTES, OCTOPUS_DISPATCH_HOLD_MINUTES, OCTOPUS_DISPATCH_TAIL_MINUTES

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
    "octopus_slots_held_until",
    "octopus_slots_held_charged",
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
    "dynamic_load_car_confirmed_periods",
    "car_charging_now",
    "dynamic_load_car_sensors",
    "car_energy_reported_load",
    "load_minutes",
    "load_last_period",
    "battery_rate_max_discharge",
)

# The Octopus component's dispatch sensor for the car, carrying the device's currentState
DISPATCH_SENSOR = "binary_sensor.predbat_test_hold_intelligent_dispatch"
# The car's car_charging_now sensor, read live by the 15 second poll
CAR_SENSOR = "binary_sensor.predbat_test_hold_car_charging"
# The house load_power sensor, in W
LOAD_SENSOR = "sensor.predbat_test_hold_load_power"


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
    my_predbat.octopus_slots_held_until = {}
    my_predbat.octopus_slots_held_charged = {}
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
    my_predbat.dynamic_load_car_confirmed_periods = {}
    my_predbat.car_charging_now = [False]
    my_predbat.dynamic_load_car_sensors = {}
    # The car inside the CT clamp, as create_predbat() has it: the cost tests that share this reset price the car's
    # energy as already in the import (see car_outside_clamp_energy()). No load_minutes keeps the load evidence off
    # (see octopus_car_charging_reading()) until a test sets them
    my_predbat.car_energy_reported_load = True
    my_predbat.load_minutes = {}
    my_predbat.load_last_period = 0
    # 6kW, so the battery's own rate never decides whether the load is low (see dynamic_load_classify())
    my_predbat.battery_rate_max_discharge = 0.1
    my_predbat.args["octopus_intelligent_slot"] = [DISPATCH_SENSOR]
    my_predbat.args.pop("intelligent_dispatch_billing", None)
    my_predbat.args.pop("load_power", None)
    _device_state(my_predbat, None)


def _device_state(my_predbat, state):
    """
    Publish the Octopus device's currentState on the car's dispatch sensor, as the Octopus component does.
    """
    my_predbat.set_state_wrapper(DISPATCH_SENSOR, "off", attributes={"current_state": state})


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
    failed |= _check(
        "t2 only the half hour not yet listed is held, and the part of the one in progress the running dispatch has gone past",
        [slot["start"] for slot in _held(my_predbat)] == [_dispatch(my_predbat, 1080, 1110)["start"], _dispatch(my_predbat, 1110, 1123)["start"]],
        "held {}".format(_held(my_predbat)),
    )
    failed |= _check("t2 the half hour in progress is held up to the running dispatch's start", _held(my_predbat)[-1]["end"] == _dispatch(my_predbat, 1110, 1123)["end"], "held {}".format(_held(my_predbat)))
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

    print("Test 13: a held half hour only minutes in is not taken for a short dispatch (OCTOPUS_SLOT_MIN_MINUTES)")
    _reset(my_predbat)
    my_predbat.minutes_now = 1115
    held = dict(_dispatch(my_predbat, 1110, 1112), held=True)
    held.pop("charge_in_kwh")
    rates = my_predbat.rate_add_io_slots(0, {minute: RATE_DAY for minute in range(-2 * 24 * 60, 2 * 24 * 60)}, [held])
    failed |= _check("t13 held two minutes keeps the half hour cheap", _low(rates, 1110, 1140), "rate {}".format(rates[1110]))
    rates = my_predbat.rate_add_io_slots(0, {minute: RATE_DAY for minute in range(-2 * 24 * 60, 2 * 24 * 60)}, [_dispatch(my_predbat, 1110, 1112)])
    failed |= _check("t13 the same two minutes as a dispatch are not", _day(rates, 1110, 1140), "rate {}".format(rates[1110]))

    print("Test 14: a car charging in a smart charge with no dispatch listed holds its half hour (the plug-in charge)")
    # Octopus starts charging as soon as the car is plugged in but never lists that half hour as planned, only as
    # completed an hour or so later: seen on three of six Ohme instances, the plug-in half hour billed off-peak
    _reset(my_predbat)
    _device_state(my_predbat, "SMART_CONTROL_IN_PROGRESS")
    my_predbat.car_charging_now = [True]
    _cycle(my_predbat, 1085)
    failed |= _check("t14 half hour recorded", my_predbat.octopus_slots_held.get(0), "held {}".format(my_predbat.octopus_slots_held))
    rates = _cycle(my_predbat, 1095)
    failed |= _check("t14 half hour in progress cheap", _low(rates, 1080, 1110), "rate {}".format(rates[1080]))
    failed |= _check("t14 no car slot planned from it", not [slot for slot in my_predbat.car_charging_slots[0] if slot["end"] > my_predbat.minutes_now], "slots {}".format(my_predbat.car_charging_slots[0]))
    my_predbat.car_charging_now = [False]
    rates = _cycle(my_predbat, 1115)
    failed |= _check("t14 held once it has passed", _low(rates, 1080, 1110), "rate {}".format(rates[1080]))
    failed |= _check("t14 next half hour not cheap", _day(rates, 1110, 1140), "rate {}".format(rates[1110]))

    for state in (None, "SMART_CONTROL_CAPABLE", "BOOSTING", "SMART_CONTROL_NOT_AVAILABLE"):
        print("Test 15: a car charging with the device {} holds nothing".format(state))
        _reset(my_predbat)
        _device_state(my_predbat, state)
        my_predbat.car_charging_now = [True]
        _cycle(my_predbat, 1085)
        rates = _cycle(my_predbat, 1095)
        failed |= _check("t15 nothing recorded for {}".format(state), not my_predbat.octopus_slots_held.get(0), "held {}".format(my_predbat.octopus_slots_held))
        failed |= _check("t15 day rate for {}".format(state), _day(rates, 1080, 1110), "rate {}".format(rates[1080]))

    print("Test 16: a smart charge in progress with the car not charging holds nothing")
    _reset(my_predbat)
    _device_state(my_predbat, "SMART_CONTROL_IN_PROGRESS")
    _cycle(my_predbat, 1085)
    failed |= _check("t16 nothing recorded", not my_predbat.octopus_slots_held.get(0), "held {}".format(my_predbat.octopus_slots_held))

    print("Test 17: a half hour slot confirmation cancelled is not held for the car charging either")
    _reset(my_predbat)
    _device_state(my_predbat, "SMART_CONTROL_IN_PROGRESS")
    my_predbat.car_charging_now = [True]
    _cycle(my_predbat, 1085, cancelled=True)
    failed |= _check("t17 nothing recorded", not my_predbat.octopus_slots_held.get(0), "held {}".format(my_predbat.octopus_slots_held))

    for slot_config in ([DISPATCH_SENSOR], DISPATCH_SENSOR):
        print("Test 18: a second car charging with no slot sensor of its own ({}) holds nothing, and logs nothing".format(type(slot_config).__name__))
        # Only car 0 is on the Intelligent tariff: car 1 must neither read car 0's dispatch sensor, which a single
        # entity resolves to at any index, nor warn every cycle and 15 second poll that it has no entry of its own
        _reset(my_predbat)
        my_predbat.num_cars = 2
        my_predbat.args["octopus_intelligent_slot"] = slot_config
        _device_state(my_predbat, "SMART_CONTROL_IN_PROGRESS")
        my_predbat.car_charging_now = [False, True]
        logged = []
        original_log = my_predbat.log
        my_predbat.log = lambda msg, *args, **kwargs: logged.append(msg)
        try:
            _cycle(my_predbat, 1085)
        finally:
            my_predbat.log = original_log
        failed |= _check("t18 nothing held for car 1 ({})".format(type(slot_config).__name__), not my_predbat.octopus_slots_held.get(1), "held {}".format(my_predbat.octopus_slots_held))
        failed |= _check("t18 no warning ({})".format(type(slot_config).__name__), not [msg for msg in logged if "octopus_intelligent_slot" in msg], "logged {}".format(logged))

    print("Test 19: the 15 second poll reads the car_charging_now sensor, not the snapshot from the last cycle")
    # The car stops just before 18:30 and the poll lands in the next half hour before a cycle has refreshed
    # car_charging_now: that half hour, which the car never charged in, must not be held
    _reset(my_predbat)
    _device_state(my_predbat, "SMART_CONTROL_IN_PROGRESS")
    my_predbat.dynamic_load_car_sensors = {0: CAR_SENSOR}
    my_predbat.set_state_wrapper(CAR_SENSOR, "on")
    my_predbat.car_charging_now = [True]
    _cycle(my_predbat, 1105)
    next_half_hour = my_predbat.midnight_utc + timedelta(minutes=1110)
    failed |= _check("t19 charging half hour held", my_predbat.octopus_slots_held.get(0), "held {}".format(my_predbat.octopus_slots_held))
    my_predbat.set_state_wrapper(CAR_SENSOR, "off")
    my_predbat.octopus_held_update(next_half_hour + timedelta(seconds=10))
    failed |= _check("t19 next half hour not held from a stale snapshot", next_half_hour not in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    # ...and the other way round: the car starting between cycles is seen on the poll, once past the tail of the
    # half hour held before it (see test 20)
    my_predbat.set_state_wrapper(CAR_SENSOR, "on")
    my_predbat.car_charging_now = [False]
    my_predbat.octopus_held_update(next_half_hour + timedelta(minutes=OCTOPUS_DISPATCH_TAIL_MINUTES, seconds=10))
    failed |= _check("t19 car seen charging on the poll is held", next_half_hour in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    # A sensor that gives no evidence leaves the snapshot to decide
    _reset(my_predbat)
    _device_state(my_predbat, "SMART_CONTROL_IN_PROGRESS")
    my_predbat.dynamic_load_car_sensors = {0: CAR_SENSOR}
    my_predbat.set_state_wrapper(CAR_SENSOR, "unavailable")
    my_predbat.car_charging_now = [True]
    _cycle(my_predbat, 1105)
    failed |= _check("t19 snapshot used when the sensor is unavailable", my_predbat.octopus_slots_held.get(0), "held {}".format(my_predbat.octopus_slots_held))

    print("Test 20: the charging tail of a dispatch that ended at the boundary does not hold the next half hour (GH#5884)")
    # The dispatch ends at 18:30, the cycle at 18:30:00 still reads the car charging (the sensor dropped 10 seconds
    # later) and Octopus still reports the smart charge in progress: 18:30-19:00 was never charged in, so stays at the day rate
    _reset(my_predbat)
    _device_state(my_predbat, "SMART_CONTROL_IN_PROGRESS")
    my_predbat.dynamic_load_car_sensors = {0: CAR_SENSOR}
    my_predbat.set_state_wrapper(CAR_SENSOR, "on")
    my_predbat.car_charging_now = [True]
    dispatch = _dispatch(my_predbat, 1089, 1110, kwh=1.2, location=None)
    _cycle(my_predbat, 1105, planned=[dispatch])
    _cycle(my_predbat, 1110, planned=[dispatch], seconds=1)
    next_half_hour = my_predbat.midnight_utc + timedelta(minutes=1110)
    failed |= _check("t20 dispatch half hour held", my_predbat.midnight_utc + timedelta(minutes=1080) in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    failed |= _check("t20 next half hour not held at the boundary", next_half_hour not in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    # A slower sensor lagging by minutes rather than seconds, then going unavailable so the stale snapshot from the
    # boundary cycle decides: still the tail
    my_predbat.octopus_held_update(next_half_hour + timedelta(minutes=3))
    failed |= _check("t20 nor on a poll minutes later", next_half_hour not in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    my_predbat.set_state_wrapper(CAR_SENSOR, "unavailable")
    my_predbat.octopus_held_update(next_half_hour + timedelta(minutes=4))
    failed |= _check("t20 nor from the boundary snapshot", next_half_hour not in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    my_predbat.set_state_wrapper(CAR_SENSOR, "off")
    my_predbat.car_charging_now = [False]
    rates = _cycle(my_predbat, 1115)
    failed |= _check("t20 dispatch half hour still cheap", _low(rates, 1080, 1110), "rate {}".format(rates[1080]))
    failed |= _check("t20 next half hour at the day rate", _day(rates, 1110, 1140), "rate {}".format(rates[1110]))

    print("Test 21: a car still charging past the tail of a dispatch holds its half hour as an unlisted smart charge")
    _reset(my_predbat)
    _device_state(my_predbat, "SMART_CONTROL_IN_PROGRESS")
    my_predbat.car_charging_now = [True]
    dispatch = _dispatch(my_predbat, 1089, 1110, kwh=1.2, location=None)
    _cycle(my_predbat, 1105, planned=[dispatch])
    _cycle(my_predbat, 1110, planned=[dispatch])
    failed |= _check("t21 not held in the tail", next_half_hour not in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    _cycle(my_predbat, 1110 + OCTOPUS_DISPATCH_TAIL_MINUTES)
    failed |= _check("t21 held at the cycle past the tail", my_predbat.octopus_slots_held.get(0, {}).get(next_half_hour) == "charging-unlisted", "held {}".format(my_predbat.octopus_slots_held))

    print("Test 22: a car plugged in at the boundary, with no dispatch before it, is held at that cycle")
    # Not a tail, so an install without the 15 second poll still holds a plug-in charge seen only at the boundary cycle
    _reset(my_predbat)
    _device_state(my_predbat, "SMART_CONTROL_IN_PROGRESS")
    my_predbat.car_charging_now = [True]
    _cycle(my_predbat, 1110, seconds=1)
    failed |= _check("t22 held at the boundary", next_half_hour in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))

    print("Test 23: the tail of a dispatch ending at midnight does not hold 00:00-00:30")
    # The half hour before is yesterday's 23:30, found by its absolute start once midnight_utc has moved on
    _reset(my_predbat)
    _device_state(my_predbat, "SMART_CONTROL_IN_PROGRESS")
    my_predbat.dynamic_load_car_sensors = {0: CAR_SENSOR}
    my_predbat.set_state_wrapper(CAR_SENSOR, "on")
    my_predbat.car_charging_now = [True]
    _cycle(my_predbat, 1435, planned=[_dispatch(my_predbat, 1425, 1440, kwh=1.2, location=None)])
    my_predbat.midnight_utc = my_predbat.midnight_utc + timedelta(days=1)
    _cycle(my_predbat, 0, seconds=1)
    failed |= _check("t23 yesterday's 23:30 held", my_predbat.midnight_utc - timedelta(minutes=30) in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    failed |= _check("t23 00:00 not held at the boundary", my_predbat.midnight_utc not in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    my_predbat.octopus_held_update(my_predbat.midnight_utc + timedelta(minutes=3))
    failed |= _check("t23 nor on a poll in the tail", my_predbat.midnight_utc not in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    my_predbat.set_state_wrapper(CAR_SENSOR, "off")
    my_predbat.car_charging_now = [False]
    rates = _cycle(my_predbat, 5)
    failed |= _check("t23 23:30-00:00 still cheap", _low(rates, -30, 0), "rate {}".format(rates[-30]))
    failed |= _check("t23 00:00-00:30 at the day rate", _day(rates, 0, 30), "rate {}".format(rates[0]))

    failed |= _run_withdrawn(my_predbat)
    return failed


def _withdrawn_setup(my_predbat, sensor="off"):
    """
    The GH#5957 set-up: a planned dispatch 19:30-20:00, the car's car_charging_now sensor reading sensor, and
    the 19:30 half hour held at the boundary cycle, before the car could be seen charging in it. Returns the
    dispatch and the absolute start of the half hour.
    """
    _reset(my_predbat)
    my_predbat.dynamic_load_car_sensors = {0: CAR_SENSOR}
    my_predbat.set_state_wrapper(CAR_SENSOR, sensor)
    dispatch = _dispatch(my_predbat, 1170, 1200, kwh=3.45, source="SMART", location=None)
    _cycle(my_predbat, 1145, planned=[dispatch])
    _cycle(my_predbat, 1170, planned=[dispatch])
    return dispatch, my_predbat.midnight_utc + timedelta(minutes=1170)


def _run_withdrawn(my_predbat):
    """
    The withdrawn dispatch scenarios (GH#5957), returning True when any check failed.
    """
    failed = False

    print("Test 24: a dispatch withdrawn just as it starts, with the car not charging, loses the half hour (the #5957 report)")
    dispatch, half_hour = _withdrawn_setup(my_predbat)
    failed |= _check("t24 held at the boundary", half_hour in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    rates = _cycle(my_predbat, 1175)
    failed |= _check("t24 day rate in the cycle that sees the withdrawal", _day(rates, 1170, 1200), "rates {} {}".format(rates[1170], rates[1199]))
    failed |= _check("t24 record let go", half_hour not in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    rates = _cycle(my_predbat, 1180)
    failed |= _check("t24 still the day rate a cycle later", _day(rates, 1170, 1200), "rate {}".format(rates[1180]))

    print("Test 25: a withdrawn dispatch the car was confirmed charging in keeps the half hour (GH#5316)")
    dispatch, half_hour = _withdrawn_setup(my_predbat)
    my_predbat.dynamic_load_car_confirmed_periods = {0: {half_hour + timedelta(minutes=30)}}
    rates = _cycle(my_predbat, 1175)
    failed |= _check("t25 held", _low(rates, 1170, 1200), "rate {}".format(rates[1175]))
    _withdrawn_setup(my_predbat)
    my_predbat.dynamic_load_car_confirmed = {0: half_hour + timedelta(minutes=30)}
    rates = _cycle(my_predbat, 1175)
    failed |= _check("t25 held from the latest confirmation", _low(rates, 1170, 1200), "rate {}".format(rates[1175]))

    print("Test 26: a withdrawn dispatch with the car still charging, or no reading, keeps the half hour")
    for state in ("on", "unavailable"):
        _withdrawn_setup(my_predbat, sensor=state)
        rates = _cycle(my_predbat, 1175)
        failed |= _check("t26 held with the sensor {}".format(state), _low(rates, 1170, 1200), "rate {}".format(rates[1175]))
    _withdrawn_setup(my_predbat)
    my_predbat.dynamic_load_car_sensors = {}
    rates = _cycle(my_predbat, 1175)
    failed |= _check("t26 held with no sensor and the car outside the CT clamp", _low(rates, 1170, 1200), "rate {}".format(rates[1175]))

    print("Test 27: a dispatch that ran to its end part-way through the half hour keeps the whole of it")
    _reset(my_predbat)
    my_predbat.dynamic_load_car_sensors = {0: CAR_SENSOR}
    my_predbat.set_state_wrapper(CAR_SENSOR, "off")
    _cycle(my_predbat, 1170, planned=[_dispatch(my_predbat, 1170, 1178)])
    rates = _cycle(my_predbat, 1180)
    failed |= _check("t27 held", _low(rates, 1170, 1200), "rate {}".format(rates[1180]))

    print("Test 28: a dispatch moved within the half hour still prices it")
    dispatch, half_hour = _withdrawn_setup(my_predbat)
    rates = _cycle(my_predbat, 1175, planned=[_dispatch(my_predbat, 1185, 1215, source="SMART", location=None)])
    failed |= _check("t28 cheap", _low(rates, 1170, 1200), "rate {}".format(rates[1175]))
    failed |= _check("t28 record kept", half_hour in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))

    print("Test 29: with slot confirmation off, or under the window rule, a withdrawn dispatch keeps the half hour")
    _withdrawn_setup(my_predbat)
    my_predbat.octopus_intelligent_dynamic = False
    rates = _cycle(my_predbat, 1175)
    failed |= _check("t29 held with octopus_intelligent_dynamic off", _low(rates, 1170, 1200), "rate {}".format(rates[1175]))
    _withdrawn_setup(my_predbat)
    my_predbat.args["intelligent_dispatch_billing"] = "window"
    rates = _cycle(my_predbat, 1175)
    failed |= _check("t29 held under the window rule", _low(rates, 1170, 1200), "rate {}".format(rates[1175]))
    my_predbat.args.pop("intelligent_dispatch_billing", None)

    print("Test 30: a car inside the CT clamp is judged on the load, as the dispatch check judges it")
    # Low is under DYNAMIC_LOAD_CAR_LOW_FACTOR of the 6kW threshold; between that and the threshold (a slow charger,
    # a car ramping up) the car may be charging, so the half hour is kept
    for load, expect_held in ((0.2, False), (5.5, True), (10.0, True)):
        _withdrawn_setup(my_predbat)
        my_predbat.dynamic_load_car_sensors = {}
        my_predbat.car_energy_reported_load = True
        my_predbat.load_minutes = {0: 1.0}
        my_predbat.load_last_period = load
        rates = _cycle(my_predbat, 1175)
        failed |= _check("t30 load {} held {}".format(load, expect_held), _low(rates, 1170, 1200) == expect_held, "rate {}".format(rates[1175]))
    # A low window with the live load not low is no evidence (GH#5461)
    _withdrawn_setup(my_predbat)
    my_predbat.dynamic_load_car_sensors = {}
    my_predbat.car_energy_reported_load = True
    my_predbat.load_minutes = {0: 1.0}
    my_predbat.load_last_period = 0.2
    my_predbat.args["load_power"] = LOAD_SENSOR
    my_predbat.set_state_wrapper(LOAD_SENSOR, "7000")
    rates = _cycle(my_predbat, 1175)
    failed |= _check("t30 held while the live load is not low", _low(rates, 1170, 1200), "rate {}".format(rates[1175]))
    my_predbat.args.pop("load_power", None)
    # The load over the 5 minutes before the boundary is the previous half hour's, so says nothing about this one
    _withdrawn_setup(my_predbat)
    my_predbat.dynamic_load_car_sensors = {}
    my_predbat.car_energy_reported_load = True
    my_predbat.load_minutes = {0: 1.0}
    my_predbat.load_last_period = 0.2
    my_predbat.octopus_slots_planned = {0: []}
    my_predbat.octopus_held_update(half_hour + timedelta(minutes=2))
    failed |= _check("t30 no judgement from the load before the half hour", half_hour in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))

    print("Test 31: the 15 second poll lets the half hour go once the car reads not charging, and asks for a replan")
    _withdrawn_setup(my_predbat, sensor="unavailable")
    _cycle(my_predbat, 1175)
    failed |= _check("t31 kept while there is no reading", half_hour in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    my_predbat.set_state_wrapper(CAR_SENSOR, "off")
    released = my_predbat.octopus_held_update(half_hour + timedelta(minutes=6))
    failed |= _check("t31 let go on the poll", half_hour not in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    failed |= _check("t31 reported for a replan", released is True, "returned {}".format(released))
    released = my_predbat.octopus_held_update(half_hour + timedelta(minutes=6, seconds=15))
    failed |= _check("t31 reported once", released is False, "returned {}".format(released))
    # A "not charging" reading only counts once the car and charger have had DYNAMIC_LOAD_CAR_START_MINUTES to wake up,
    # as the dispatch check takes it
    _withdrawn_setup(my_predbat)
    my_predbat.octopus_slots_planned = {0: []}
    my_predbat.octopus_held_update(half_hour + timedelta(minutes=DYNAMIC_LOAD_CAR_START_MINUTES - 1))
    failed |= _check("t31 kept while the car may still be waking", half_hour in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    my_predbat.octopus_held_update(half_hour + timedelta(minutes=DYNAMIC_LOAD_CAR_START_MINUTES))
    failed |= _check("t31 let go once it has had time", half_hour not in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))
    # The poll does not judge the load: the window is the last cycle's
    _withdrawn_setup(my_predbat)
    my_predbat.dynamic_load_car_sensors = {}
    my_predbat.car_energy_reported_load = True
    my_predbat.load_minutes = {0: 1.0}
    my_predbat.load_last_period = 0.2
    my_predbat.minutes_now = 1175
    my_predbat.octopus_slots_planned = {0: []}
    my_predbat.octopus_held_update(half_hour + timedelta(minutes=6))
    failed |= _check("t31 the poll leaves the load to the cycle", half_hour in my_predbat.octopus_slots_held.get(0, {}), "held {}".format(my_predbat.octopus_slots_held))

    print("Test 32: a long dispatch withdrawn later lets go only the half hour in progress")
    _reset(my_predbat)
    my_predbat.dynamic_load_car_sensors = {0: CAR_SENSOR}
    my_predbat.set_state_wrapper(CAR_SENSOR, "on")
    dispatch = _dispatch(my_predbat, 1140, 1260, kwh=10.0, source="SMART", location=None)
    _cycle(my_predbat, 1140, planned=[dispatch])
    _cycle(my_predbat, 1165, planned=[dispatch])
    # The car is full before 19:30, so is never seen charging in the 19:30 half hour
    my_predbat.set_state_wrapper(CAR_SENSOR, "off")
    _cycle(my_predbat, 1170, planned=[dispatch])
    rates = _cycle(my_predbat, 1175)
    failed |= _check("t32 earlier half hour still held", _low(rates, 1140, 1170), "rate {}".format(rates[1140]))
    failed |= _check("t32 half hour in progress let go", _day(rates, 1170, 1200), "rate {}".format(rates[1175]))

    print("Test 33: after a restart the dispatch's end is not known, so the hold is kept as before")
    _withdrawn_setup(my_predbat)
    my_predbat.octopus_slots_held_until = {}
    rates = _cycle(my_predbat, 1175)
    failed |= _check("t33 held", _low(rates, 1170, 1200), "rate {}".format(rates[1175]))

    print("Test 34: a car seen charging in the half hour before it was confirmed keeps it (filled up, then Octopus ended the dispatch)")
    # Confirmation waits DYNAMIC_LOAD_CAR_CONFIRM_MINUTES into the half hour; the car charged for 90 seconds before that
    dispatch, half_hour = _withdrawn_setup(my_predbat, sensor="on")
    my_predbat.octopus_held_update(half_hour + timedelta(seconds=30))
    my_predbat.set_state_wrapper(CAR_SENSOR, "off")
    my_predbat.octopus_held_update(half_hour + timedelta(minutes=1, seconds=30))
    rates = _cycle(my_predbat, 1175)
    failed |= _check("t34 held", _low(rates, 1170, 1200), "rate {}".format(rates[1175]))
    # ...and seen on the load, for a car inside the CT clamp
    _withdrawn_setup(my_predbat)
    my_predbat.dynamic_load_car_sensors = {}
    my_predbat.car_energy_reported_load = True
    my_predbat.load_minutes = {0: 1.0}
    my_predbat.load_last_period = 5.5
    _cycle(my_predbat, 1175, planned=[dispatch])
    my_predbat.load_last_period = 0.2
    rates = _cycle(my_predbat, 1180)
    failed |= _check("t34 held after load that was not low", _low(rates, 1170, 1200), "rate {}".format(rates[1180]))

    return failed


def test_octopus_dispatch_hold(my_predbat):
    """
    The half hours a planned Octopus Intelligent dispatch has run in keep the dispatch rate until Octopus
    lists them as completed.
    """
    print("*** Running test: Octopus dispatch hold")
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in STATE_FIELDS}
    saved_args = {key: my_predbat.args.get(key) for key in ("octopus_slot_low_rate", "octopus_slot_max", "octopus_intelligent_slot", "intelligent_dispatch_billing", "load_power")}
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
