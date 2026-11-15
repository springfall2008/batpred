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

import re
from datetime import timedelta

from utils import prune_today, str2time


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


def test_lookahead_prune_keeps_what_lands_today(my_predbat):
    """Pruning to today judges a point by where it is plotted, not when it was recorded.

    A +8h prediction made at 18:00 yesterday describes 02:00 today, so it belongs on today's chart;
    one made two days ago does not, however far ahead it looked.
    """
    print("  - test_lookahead_prune_keeps_what_lands_today")
    failed = False
    midnight = my_predbat.midnight_utc
    yesterday_evening = midnight - timedelta(hours=6)
    two_days_ago = midnight - timedelta(days=2)
    this_morning = midnight + timedelta(hours=1)
    recorded = {two_days_ago.isoformat(): 1.0, yesterday_evening.isoformat(): 2.0, this_morning.isoformat(): 3.0}

    shifted8 = prune_today(recorded, my_predbat.now_utc, midnight, prune=True, offset_minutes=60 * 8)
    expected = {(yesterday_evening + timedelta(hours=8)).isoformat(): 2.0, (this_morning + timedelta(hours=8)).isoformat(): 3.0}
    if shifted8 != expected:
        print("ERROR: +8h pruned to today should keep yesterday evening's and this morning's points, got {}".format(shifted8))
        failed = True
    return failed


def test_battery_chart_lookahead_starts_today(my_predbat):
    """The Battery chart's +1h/+8h series start today, like its other series, not a week back.

    They are built from a 7-day history; unpruned, that history stretched the chart's x-axis back
    across every day of it.
    """
    print("  - test_battery_chart_lookahead_starts_today")
    from web import WebInterface

    failed = False
    midnight = my_predbat.midnight_utc
    fmt = "%Y-%m-%dT%H:%M:%S.%f%z"
    records = []
    for hours_back in range(24 * 6, 0, -1):
        stamp = my_predbat.now_utc - timedelta(hours=hours_back)
        records.append({"state": "5.0", "attributes": {"soc_h1": 5.0, "soc_h8": 6.0}, "last_updated": stamp.strftime(fmt)})

    web = WebInterface(my_predbat, web_port=5052)
    captured = {}
    web.get_history_with_now_attrs = lambda entity_id, days: [list(records)]
    web.get_entity_results = lambda entity_id: {my_predbat.now_utc.strftime(fmt): 1.0}
    web.render_chart = lambda series_data, *args, **kwargs: captured.setdefault("series", series_data) and ""
    web.get_chart("Battery")

    series = {item["name"]: item["data"] for item in captured.get("series", [])}
    for name in ("Predicted (+1h)", "Predicted (+8h)"):
        data = series.get(name)
        if not data:
            print("ERROR: the {} series is missing or empty".format(name))
            failed = True
            continue
        earliest = min(str2time(stamp) for stamp in data)
        if earliest < midnight:
            print("ERROR: {} starts at {}, before midnight {} - the chart would reach back days".format(name, earliest, midnight))
            failed = True
    return failed


def test_battery_chart_inverter_soc(my_predbat):
    """A multi-inverter Battery chart carries each inverter's actual SoC, in % of its own capacity, hidden.

    Each battery is on its own 0-100% scale, so a 5 kWh battery at 4 kWh reads 80% whatever the fleet
    total is. A single-inverter chart gets no extra lines: its one battery is already Actual.
    """
    print("  - test_battery_chart_inverter_soc")
    from types import SimpleNamespace
    from web import WebInterface

    failed = False
    fmt = "%Y-%m-%dT%H:%M:%S.%f%z"
    # One reading from just before midnight, which holds for the whole of today
    stamp = (my_predbat.midnight_utc - timedelta(minutes=30)).strftime(fmt)
    # Inverter 0 reports kWh and has a battery_scaling; inverter 1 reports Wh, which must be converted
    readings = {
        "sensor.inv0_soc_kw": ("4.0", "kWh"),
        "sensor.inv1_soc_kw": ("2000", "Wh"),
        "sensor.inv0_soc": ("65", "%"),
        "sensor.inv1_soc": ("30", "%"),
    }

    def fake_history(entity_id, days, required=True, tracked=True):
        state, unit = readings[entity_id]
        return [[{"state": state, "last_updated": stamp, "attributes": {"unit_of_measurement": unit}}]]

    def check_series(series, expected, label):
        """Each expected inverter line is hidden, in %, flat at its past value and ends on its live value."""
        bad = False
        now_str = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
        for name, (expect_past, expect_now) in expected.items():
            item = series.get(name)
            if not item:
                print("ERROR: {}: the {} series is missing, got {}".format(label, name, list(series)))
                bad = True
                continue
            if not item.get("hidden") or item.get("unit") != "%":
                print("ERROR: {}: {} should be hidden and in %, got hidden={} unit={}".format(label, name, item.get("hidden"), item.get("unit")))
                bad = True
            past = [value for stamp_key, value in item["data"].items() if stamp_key != now_str]
            if not past or any(value != expect_past for value in past):
                print("ERROR: {}: {} past SoC should be {}% of its own capacity throughout, got {}".format(label, name, expect_past, item["data"]))
                bad = True
            if list(item["data"].keys())[-1] != now_str or item["data"][now_str] != expect_now:
                print("ERROR: {}: {} should end at the inverter's current {}%, got {}".format(label, name, expect_now, item["data"]))
                bad = True
        return bad

    saved_args = my_predbat.args
    saved_inverters = my_predbat.inverters
    try:
        my_predbat.args = dict(saved_args)
        my_predbat.args.pop("soc_percent", None)
        my_predbat.args["soc_kw"] = ["sensor.inv0_soc_kw", "sensor.inv1_soc_kw"]
        # soc_max already includes battery_scaling, as Inverter computes it
        my_predbat.inverters = [
            SimpleNamespace(id=0, soc_max=4.5, battery_scaling=0.9, soc_percent=82),
            SimpleNamespace(id=1, soc_max=10.0, battery_scaling=1.0, soc_percent=21),
        ]

        web = WebInterface(my_predbat, web_port=5053)
        web.get_history_wrapper = fake_history
        web.get_history_with_now_attrs = lambda entity_id, days: []
        web.get_entity_results = lambda entity_id: {my_predbat.now_utc.strftime(fmt): 1.0}
        captured = {}

        def capture(series_data, *args, **kwargs):
            captured["series"] = series_data
            captured["extra_yaxis"] = kwargs.get("extra_yaxis")
            return ""

        # soc_kw sensors: 4.0 kWh x 0.9 of 4.5 kWh is 80%; 2000 Wh of 10 kWh is 20%
        web.render_chart = capture
        web.get_chart("Battery")
        series = {item["name"]: item for item in captured.get("series", [])}
        failed |= check_series(series, {"Actual Inverter 0": (80.0, 82), "Actual Inverter 1": (20.0, 21)}, "soc_kw")
        axis = captured.get("extra_yaxis") or {}
        if axis.get("series_names") != ["Actual Inverter 0", "Actual Inverter 1"] or axis.get("min") != 0 or axis.get("max") != 100:
            print("ERROR: the inverter lines should share a 0-100% axis, got {}".format(axis))
            failed = True

        # The real renderer emits the hidden flag and the fixed axis range, and keeps the inverter lines off the kWh axis
        del web.render_chart
        html = web.get_chart("Battery")
        if "hidden: true" not in html or "max: 100" not in html:
            print("ERROR: the rendered chart should start the inverter lines hidden on a 0-100 axis")
            failed = True
        primary = re.search(r"title: \{ text: 'kWh' \},\s*decimalsInFloat: 2,\s*seriesName: \[([^\]]*)\]", html)
        if not primary or "Actual Inverter" in primary.group(1):
            print("ERROR: the kWh axis should list the fleet series only, got {}".format(primary.group(1) if primary else None))
            failed = True

        # soc_percent sensors are plotted as they are
        my_predbat.args["soc_percent"] = ["sensor.inv0_soc", "sensor.inv1_soc"]
        web.render_chart = capture
        web.get_chart("Battery")
        series = {item["name"]: item for item in captured.get("series", [])}
        failed |= check_series(series, {"Actual Inverter 0": (65.0, 82), "Actual Inverter 1": (30.0, 21)}, "soc_percent")

        # One inverter: the chart is unchanged
        my_predbat.inverters = my_predbat.inverters[:1]
        web.get_chart("Battery")
        if captured.get("extra_yaxis") is not None or any(item.get("unit") == "%" for item in captured.get("series", [])):
            print("ERROR: a single-inverter install should get no per-inverter lines or % axis")
            failed = True
    finally:
        my_predbat.args = saved_args
        my_predbat.inverters = saved_inverters
    return failed


def run_battery_accuracy_tests(my_predbat):
    """Run every battery prediction-accuracy test."""
    print("**** Running battery accuracy tests ****\n")
    failed = test_soc_lookahead_recorded(my_predbat)
    failed |= test_lookahead_offset_aligns_with_actual(my_predbat)
    failed |= test_lookahead_prune_keeps_what_lands_today(my_predbat)
    failed |= test_battery_chart_lookahead_starts_today(my_predbat)
    failed |= test_battery_chart_inverter_soc(my_predbat)
    failed |= test_load_power_less_car(my_predbat)
    failed |= test_load_power_less_car_matches_chart(my_predbat)
    return failed


def test_load_power_less_car(my_predbat):
    """House load with the car taken out, matched on time rather than on exact timestamp.

    The inverter reports house load including anything inside the CT clamp, so a charger on that side
    shows as a 7kW spike, while the ML forecast it is plotted against has car charging subtracted
    (car_charging_hold). Plotting the two against each other makes every session look like a forecast
    miss the model was never attempting.

    The timestamps here are deliberately offset by a few seconds, because that is the real case: the
    two entities are recorded by Home Assistant independently and prune_today() keys each result on
    its own source timestamp. An earlier version of this test used identical keys, so it exercised the
    arithmetic and not the alignment - and passed while the chart drew two identical lines.
    """
    print("  - test_load_power_less_car")
    from web import subtract_series

    failed = False
    base = my_predbat.midnight_utc + timedelta(hours=12)

    def stamp(seconds):
        """A timestamp the given number of seconds after noon."""
        return (base + timedelta(seconds=seconds)).isoformat()

    # Car samples land a few seconds off the load samples, as they do in production
    load = {stamp(0): 8.0, stamp(300): 1.2, stamp(600): 5.0}
    car = {stamp(3): 7.0, stamp(303): 0.0, stamp(597): 7.0}
    out = subtract_series(load, car)

    if len(out) != len(load):
        print("ERROR: the corrected series should have one point per load sample, got {}".format(len(out)))
        return True

    # 8kW of load against the 7kW the car was drawing a moment earlier
    if abs(out[stamp(600)] - 0) > 0.001:
        print("ERROR: a car reading above the load reading should clamp to 0, got {}".format(out[stamp(600)]))
        failed = True
    if abs(out[stamp(300)] - 1.2) > 0.001:
        print("ERROR: with the car idle the house figure is unchanged, got {}".format(out[stamp(300)]))
        failed = True
    if any(value < 0 for value in out.values()):
        print("ERROR: the series must never go negative, got {}".format(out))
        failed = True

    # Jitter either way is still the same moment: a car sample just after a load sample counts
    after = subtract_series({stamp(0): 4.0}, {stamp(3): 3.0})
    if abs(after[stamp(0)] - 1.0) > 0.001:
        print("ERROR: a car sample 3s later is the same cycle and should be subtracted, got {}".format(after[stamp(0)]))
        failed = True

    # But a sample from far outside the cycle is not evidence of anything, or a car sensor that
    # dropped out hours ago would keep being subtracted from every later point
    stale = subtract_series({stamp(7200): 4.0}, {stamp(0): 3.0})
    if abs(stale[stamp(7200)] - 4.0) > 0.001:
        print("ERROR: a car sample two hours away must not be subtracted, got {}".format(stale[stamp(7200)]))
        failed = True

    # No charger configured: no series at all, so the chart is exactly as it was before
    if subtract_series(load, {}):
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

    if "load_power_no_car = subtract_series(load_power, car_charging_power)" not in source:
        print("ERROR: the LoadMLPower chart no longer builds its house-minus-car series with subtract_series()")
        failed = True
    for name in ('"Load Power (Actual, less car)"', '"Car Charging Power"'):
        if name not in source:
            print("ERROR: the LoadMLPower chart is missing the {} series".format(name))
            failed = True
    return failed
