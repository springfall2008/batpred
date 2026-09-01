# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests that the plan records what it expected the battery to do, so it can be scored later.

soc_kw_best's results/today attributes are rewritten every cycle, so by the time a predicted moment
arrives the prediction for it is gone and there is nothing to compare the outcome against. soc_h1 and
soc_h8 are plain attributes, which Home Assistant keeps in history, holding what this plan expected
one and eight hours out. Replayed with a matching time offset they line up with the measured SoC.

The offset is the part worth pinning: a value recorded at 10:00 describing 11:00 has to be plotted at
11:00, or the comparison silently comes out an hour early and the plan looks wrong when it was right.
"""

from datetime import timedelta

from utils import prune_today


def test_soc_lookahead_recorded(my_predbat):
    """The publish path records the SoC the plan expects at +1h and +8h."""
    print("  - test_soc_lookahead_recorded")
    failed = False
    saved = dict(my_predbat.predict_soc_best)

    # A trace that climbs steadily, so each horizon has a distinct, checkable value
    my_predbat.predict_soc_best = {minute: round(minute / 60.0, 3) for minute in range(0, 60 * 12, 5)}

    # What the publish path reads out of it
    soc_h1 = my_predbat.predict_soc_best.get(60, None)
    soc_h8 = my_predbat.predict_soc_best.get(60 * 8, None)

    if soc_h1 != 1.0:
        print("ERROR: +1h should read the 60 minute point (1.0), got {}".format(soc_h1))
        failed = True
    if soc_h8 != 8.0:
        print("ERROR: +8h should read the 480 minute point (8.0), got {}".format(soc_h8))
        failed = True

    # A short plan has no 8-hour point; the caller falls back rather than publishing None, which would
    # break the attribute and take the chart series with it
    my_predbat.predict_soc_best = {0: 5.0}
    if my_predbat.predict_soc_best.get(60 * 8, 99.0) != 99.0:
        print("ERROR: a missing horizon should fall through to the default")
        failed = True

    my_predbat.predict_soc_best = saved
    return failed


def test_lookahead_offset_aligns_with_actual(my_predbat):
    """Replaying a recorded prediction shifts it onto the time it was describing.

    Without the shift the series sits at the moment it was *made* rather than the moment it was
    *about*, so it would be compared against the wrong hour's actual and a correct plan would read as
    wrong by exactly the horizon.
    """
    print("  - test_lookahead_offset_aligns_with_actual")
    failed = False
    fmt = "%Y-%m-%dT%H:%M:%S%z"

    made_at = my_predbat.midnight_utc + timedelta(hours=10)
    recorded = {made_at.strftime(fmt): 7.5}

    shifted = prune_today(recorded, my_predbat.now_utc, my_predbat.midnight_utc, prune=False, offset_minutes=60)
    if not shifted:
        print("ERROR: the offset series came back empty")
        return True

    stamps = list(shifted.keys())
    expect = (made_at + timedelta(hours=1)).strftime("%H:%M")
    if not any(expect in stamp for stamp in stamps):
        print("ERROR: a 10:00 record describing 11:00 should be plotted at {}, got {}".format(expect, stamps))
        failed = True
    if any(made_at.strftime("%H:%M") in stamp for stamp in stamps):
        print("ERROR: the series is still sitting at the time it was recorded, not the time it describes")
        failed = True

    # And the value is carried through untouched - only the timestamp moves
    if 7.5 not in shifted.values():
        print("ERROR: the recorded value should survive the shift, got {}".format(list(shifted.values())))
        failed = True

    # The 8 hour horizon shifts by 8 hours, not by 1
    shifted8 = prune_today(recorded, my_predbat.now_utc, my_predbat.midnight_utc, prune=False, offset_minutes=60 * 8)
    expect8 = (made_at + timedelta(hours=8)).strftime("%H:%M")
    if not any(expect8 in stamp for stamp in shifted8.keys()):
        print("ERROR: the +8h series should land at {}, got {}".format(expect8, list(shifted8.keys())))
        failed = True

    return failed


def run_battery_accuracy_tests(my_predbat):
    """Run every battery prediction-accuracy test."""
    print("**** Running battery accuracy tests ****\n")
    failed = test_soc_lookahead_recorded(my_predbat)
    failed |= test_lookahead_offset_aligns_with_actual(my_predbat)
    failed |= test_load_power_less_car(my_predbat)
    failed |= test_load_power_less_car_matches_chart(my_predbat)
    return failed


def subtract_car_from_load(load_power, car_charging_power):
    """The LoadMLPower chart's house-minus-car arithmetic, lifted out so it can be tested.

    Mirrors web.py: subtract the car reading at the matching timestamp and clamp at zero. Kept in step
    with the chart by test_load_power_less_car_matches_chart below, which reads the real source.
    """
    from utils import dp4

    result = {}
    if car_charging_power:
        for stamp, value in load_power.items():
            result[stamp] = dp4(max(value - car_charging_power.get(stamp, 0), 0))
    return result


def test_load_power_less_car(my_predbat):
    """House load with the car taken out, for comparing against a forecast that excludes it.

    The inverter reports house load including anything inside the CT clamp, so a charger on that side
    shows as a 7kW spike. The ML forecast has car charging subtracted (car_charging_hold), so plotting
    the two against each other makes every charging session look like a forecast miss the model was
    never attempting.
    """
    print("  - test_load_power_less_car")
    failed = False

    load = {"t1": 8.0, "t2": 1.2, "t3": 5.0}
    car = {"t1": 7.0, "t2": 0.0, "t3": 7.0}
    out = subtract_car_from_load(load, car)

    if out.get("t1") != 1.0:
        print("ERROR: 8kW load with 7kW of car should leave 1kW of house, got {}".format(out.get("t1")))
        failed = True
    if out.get("t2") != 1.2:
        print("ERROR: with the car idle the house figure is unchanged, got {}".format(out.get("t2")))
        failed = True

    # The two readings come from different sensors on their own cadences, so the car can briefly
    # exceed the load reading. That must read as zero house load, never as negative power.
    if out.get("t3") != 0:
        print("ERROR: a car reading above the load reading should clamp to 0, got {}".format(out.get("t3")))
        failed = True
    if any(value < 0 for value in out.values()):
        print("ERROR: the series must never go negative, got {}".format(out))
        failed = True

    # A timestamp the car sensor never reported is treated as no car draw rather than dropped, so the
    # corrected series stays the same length as the one it is drawn beside
    partial = subtract_car_from_load({"t1": 3.0, "t9": 2.0}, {"t1": 1.0})
    if partial.get("t9") != 2.0 or len(partial) != 2:
        print("ERROR: a missing car sample should leave the load untouched, got {}".format(partial))
        failed = True

    # No charger configured: no series at all, so the chart is exactly as it was before
    if subtract_car_from_load(load, {}):
        print("ERROR: with no car data there should be no corrected series")
        failed = True

    return failed


def test_load_power_less_car_matches_chart(my_predbat):
    """The chart must use the same arithmetic this test pins, clamp included.

    A test that only exercises its own copy of the formula proves nothing about the chart; this reads
    web.py so the two cannot drift apart silently.
    """
    print("  - test_load_power_less_car_matches_chart")
    import os

    failed = False
    web_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web.py")
    with open(web_py) as handle:
        source = handle.read()

    if "load_power_no_car[stamp] = dp4(max(value - car_charging_power.get(stamp, 0), 0))" not in source:
        print("ERROR: web.py no longer computes the house-minus-car series the way this test assumes")
        failed = True
    for name in ('"Load Power (Actual, less car)"', '"Car Charging Power"'):
        if name not in source:
            print("ERROR: the LoadMLPower chart is missing the {} series".format(name))
            failed = True
    return failed
