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
Removing the discount the Octopus Energy integration's rate feed keeps for a withdrawn dispatch (GH#5663).

The feed lowers the price of dispatch minutes and marks them is_intelligent_adjusted, and was seen to keep doing
so for half an hour after Octopus withdrew the dispatch. A minute whose half hour a dispatch covered at an earlier
cycle, and none covers now, goes back to the day rate; one no dispatch has ever covered is left alone.
"""
import copy
from datetime import timedelta

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
RATE_LOW = 7.6
RATE_DAY = 35.6

STATE_FIELDS = (
    "num_cars",
    "minutes_now",
    "midnight_utc",
    "forecast_minutes",
    "rate_max_base",
    "io_adjusted",
    "octopus_slots",
    "octopus_feed_covered",
    "octopus_feed_stripped",
    "octopus_intelligent_dynamic",
    "car_charging_now",
)


def _check(name, condition, detail=""):
    """
    Print the outcome of one check, returning True when it failed.
    """
    if condition:
        return False
    print("ERROR: {} {}".format(name, detail))
    return True


def _dispatch(my_predbat, start_minute, end_minute, kwh=3.55, source="smart-charge", location="AT_HOME", held=False):
    """
    A raw Octopus dispatch entry covering start_minute to end_minute past midnight.
    """
    start = my_predbat.midnight_utc + timedelta(minutes=start_minute)
    end = my_predbat.midnight_utc + timedelta(minutes=end_minute)
    slot = {"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "charge_in_kwh": kwh, "source": source, "location": location}
    if held:
        slot["held"] = True
        slot.pop("charge_in_kwh")
    return slot


def _reset(my_predbat):
    """
    One Octopus Intelligent car, nothing seen covered yet.
    """
    my_predbat.num_cars = 1
    my_predbat.forecast_minutes = 2 * 24 * 60
    my_predbat.rate_max_base = RATE_DAY
    my_predbat.octopus_slots = [[]]
    my_predbat.octopus_feed_covered = set()
    my_predbat.octopus_feed_stripped = set()
    my_predbat.octopus_intelligent_dynamic = True
    my_predbat.car_charging_now = [False]
    my_predbat.args.pop("intelligent_dispatch_billing", None)


def _cycle(my_predbat, minute, feed, slots=None, save=True, car_slots=None):
    """
    One cycle at minute past midnight: the feed discounts the minutes in each (start, end) of feed, car 0's
    dispatches are slots, or each car's are car_slots. Returns the rates after the strip.
    """
    my_predbat.minutes_now = minute
    my_predbat.octopus_slots = [list(car) for car in car_slots] if car_slots is not None else [list(slots or [])]
    rates = {m: RATE_DAY for m in range(-24 * 60, 2 * 24 * 60)}
    my_predbat.io_adjusted = {}
    for start_minute, end_minute in feed:
        for m in range(start_minute, end_minute):
            rates[m] = RATE_LOW
            my_predbat.io_adjusted[m] = True
    return my_predbat.octopus_strip_withdrawn_feed_rates(rates, save=save)


def _low(rates, start_minute, end_minute):
    """
    Whether every minute from start_minute up to end_minute keeps the feed's discount.
    """
    return all(rates[m] == RATE_LOW for m in range(start_minute, end_minute))


def _day(rates, start_minute, end_minute):
    """
    Whether every minute from start_minute up to end_minute is at the day rate.
    """
    return all(rates[m] == RATE_DAY for m in range(start_minute, end_minute))


def _run(my_predbat):
    """
    Run the scenarios, returning True when any check failed.
    """
    failed = False

    print("Test 1: a dispatch withdrawn while the feed still discounts it goes back to the day rate (the #5663 report)")
    _reset(my_predbat)
    # 16:35: the 16:30-17:00 dispatch is listed and the feed discounts it
    rates = _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)])
    failed |= _check("t1 discounted while listed", _low(rates, 990, 1020), "rate {}".format(rates[995]))
    # 16:40: Octopus has withdrawn it, the feed still discounts 16:40-17:00
    rates = _cycle(my_predbat, 1000, [(1000, 1020)])
    failed |= _check("t1 day rate once withdrawn", _day(rates, 1000, 1020), "rates {} {}".format(rates[1000], rates[1019]))
    failed |= _check("t1 marker removed", not any(my_predbat.io_adjusted.get(m) for m in range(1000, 1020)), "io_adjusted {}".format(sorted(my_predbat.io_adjusted)[:3]))

    print("Test 2: a discount no dispatch has ever covered is left alone")
    # The feed can be ahead of the dispatching sensor, or there may be no dispatching sensor at all
    _reset(my_predbat)
    rates = _cycle(my_predbat, 1000, [(1100, 1130)])
    failed |= _check("t2 kept", _low(rates, 1100, 1130), "rate {}".format(rates[1100]))
    rates = _cycle(my_predbat, 1005, [(1100, 1130)], [_dispatch(my_predbat, 1100, 1130)])
    failed |= _check("t2 kept once listed", _low(rates, 1100, 1130), "rate {}".format(rates[1100]))

    print("Test 3: a dispatch moved later loses the old half hour and keeps the new one")
    _reset(my_predbat)
    _cycle(my_predbat, 960, [(1260, 1290)], [_dispatch(my_predbat, 1260, 1290)])
    rates = _cycle(my_predbat, 965, [(1260, 1320)], [_dispatch(my_predbat, 1290, 1320)])
    failed |= _check("t3 old half hour at the day rate", _day(rates, 1260, 1290), "rate {}".format(rates[1260]))
    failed |= _check("t3 new half hour discounted", _low(rates, 1290, 1320), "rate {}".format(rates[1290]))

    print("Test 4: a half hour covered by a completed or held dispatch keeps the discount")
    for name, slot in (("completed", _dispatch(my_predbat, 990, 1005, kwh=1.2)), ("held", _dispatch(my_predbat, 990, 1000, held=True))):
        _reset(my_predbat)
        _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)])
        rates = _cycle(my_predbat, 1000, [(1000, 1020)], [slot])
        failed |= _check("t4 {} keeps the half hour".format(name), _low(rates, 1000, 1020), "rate {}".format(rates[1000]))

    print("Test 5: elapsed minutes and the fixed overnight window are never touched")
    _reset(my_predbat)
    _cycle(my_predbat, 995, [(990, 1020), (1410, 1440)], [_dispatch(my_predbat, 990, 1020), _dispatch(my_predbat, 1410, 1440)])
    rates = _cycle(my_predbat, 1000, [(990, 1020), (1410, 1440)])
    failed |= _check("t5 elapsed minutes kept", _low(rates, 990, 1000), "rate {}".format(rates[990]))
    failed |= _check("t5 rest of the half hour at the day rate", _day(rates, 1000, 1020), "rate {}".format(rates[1000]))
    failed |= _check("t5 23:30-00:00 kept", _low(rates, 1410, 1440), "rate {}".format(rates[1410]))

    print("Test 6: under the window billing rule the discount stays")
    _reset(my_predbat)
    my_predbat.args["intelligent_dispatch_billing"] = "window"
    _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)])
    rates = _cycle(my_predbat, 1000, [(1000, 1020)])
    failed |= _check("t6 kept", _low(rates, 1000, 1020), "rate {}".format(rates[1000]))
    failed |= _check("t6 nothing recorded", not my_predbat.octopus_feed_covered, "covered {}".format(my_predbat.octopus_feed_covered))
    my_predbat.args.pop("intelligent_dispatch_billing", None)

    print("Test 7: a compare run (save False) records nothing, and a restart starts with nothing recorded")
    _reset(my_predbat)
    _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)], save=False)
    rates = _cycle(my_predbat, 1000, [(1000, 1020)])
    failed |= _check("t7 nothing recorded by a compare run", _low(rates, 1000, 1020), "rate {}".format(rates[1000]))
    _reset(my_predbat)
    rates = _cycle(my_predbat, 1000, [(1000, 1020)])
    failed |= _check("t7 nothing stripped after a restart", _low(rates, 1000, 1020), "rate {}".format(rates[1000]))

    print("Test 8: half hours seen covered are kept by absolute time across midnight, and dropped once past")
    _reset(my_predbat)
    # 06:00-06:30 tomorrow, outside the fixed window
    _cycle(my_predbat, 1430, [(1800, 1830)], [_dispatch(my_predbat, 1800, 1830)])
    saved_midnight = my_predbat.midnight_utc
    try:
        my_predbat.midnight_utc = saved_midnight + timedelta(days=1)
        rates = _cycle(my_predbat, 5, [(360, 390)])
        failed |= _check("t8 withdrawn after midnight at the day rate", _day(rates, 360, 390), "rate {}".format(rates[360]))
        _cycle(my_predbat, 400, [])
        failed |= _check("t8 past half hours dropped", not my_predbat.octopus_feed_covered, "covered {}".format(my_predbat.octopus_feed_covered))
    finally:
        my_predbat.midnight_utc = saved_midnight

    print("Test 9: logged when the stripped count changes, not every cycle")
    _reset(my_predbat)
    logged = []
    original_log = my_predbat.log
    my_predbat.log = lambda msg, *args, **kwargs: logged.append(msg)
    try:
        _cycle(my_predbat, 995, [(990, 1020)], [_dispatch(my_predbat, 990, 1020)])
        _cycle(my_predbat, 1000, [(1000, 1020)])
        # Fewer minutes as time passes is not a new strip
        _cycle(my_predbat, 1005, [(1005, 1020)])
        _cycle(my_predbat, 1010, [(1010, 1020)])
    finally:
        my_predbat.log = original_log
    messages = [msg for msg in logged if "rate feed still carries" in msg]
    failed |= _check("t9 logged once", len(messages) == 1, "logged {}".format(messages))

    print("Test 10: with the dispatch check off, the half hour in progress is kept, later ones are not")
    # Only the check's 15 second poll holds a half hour a dispatch started and was withdrawn in between two cycles
    _reset(my_predbat)
    my_predbat.octopus_intelligent_dynamic = False
    _cycle(my_predbat, 995, [(990, 1050)], [_dispatch(my_predbat, 990, 1050)])
    rates = _cycle(my_predbat, 1000, [(1000, 1050)])
    failed |= _check("t10 half hour in progress kept", _low(rates, 1000, 1020), "rate {}".format(rates[1000]))
    failed |= _check("t10 next half hour at the day rate", _day(rates, 1020, 1050), "rate {}".format(rates[1020]))

    print("Test 11: while a car reads charging nothing is stripped, as Octopus re-planning a running session empties the list for minutes")
    _reset(my_predbat)
    _cycle(my_predbat, 995, [(990, 1050)], [_dispatch(my_predbat, 990, 1050)])
    my_predbat.car_charging_now = [True]
    rates = _cycle(my_predbat, 1000, [(1000, 1050)])
    failed |= _check("t11 all kept", _low(rates, 1000, 1050), "rates {} {}".format(rates[1000], rates[1020]))
    # Once the car has stopped and the dispatch is still gone, it is stripped
    my_predbat.car_charging_now = [False]
    rates = _cycle(my_predbat, 1005, [(1005, 1050)])
    failed |= _check("t11 stripped once the car has stopped", _day(rates, 1005, 1050), "rate {}".format(rates[1005]))

    print("Test 12: a half hour another car's dispatch still covers keeps the discount")
    _reset(my_predbat)
    my_predbat.num_cars = 2
    my_predbat.car_charging_now = [False, False]
    _cycle(my_predbat, 995, [(990, 1080)], car_slots=[[_dispatch(my_predbat, 990, 1080)], []])
    rates = _cycle(my_predbat, 1000, [(1000, 1080)], car_slots=[[], [_dispatch(my_predbat, 1020, 1050)]])
    failed |= _check("t12 car 1's half hour kept", _low(rates, 1020, 1050), "rate {}".format(rates[1020]))
    failed |= _check("t12 others at the day rate", _day(rates, 1000, 1020) and _day(rates, 1050, 1080), "rates {} {}".format(rates[1000], rates[1050]))

    return failed


def test_octopus_feed_withdrawn(my_predbat):
    """
    The rate feed's discount for a withdrawn Octopus Intelligent dispatch goes back to the day rate.
    """
    print("*** Running test: Octopus feed discount for a withdrawn dispatch")
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in STATE_FIELDS}
    saved_args = {key: my_predbat.args.get(key) for key in ("intelligent_dispatch_billing",)}
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
    print("*** Octopus feed discount for a withdrawn dispatch test {}".format("FAILED" if failed else "PASSED"))
    return failed
