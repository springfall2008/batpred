# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from datetime import timedelta

from tests.test_infra import reset_rates2, reset_inverter


def set_rate_profile(my_predbat, profile, default_rate=20.0, export_rate=0.0):
    """
    Set a custom import rate profile for iBoost tests

    profile is a list of (start, end, rate) tuples applied over a flat default_rate; rates are
    filled a couple of hours past the forecast end so sliding windows never fall off the data.
    """
    for minute in range(my_predbat.forecast_minutes + my_predbat.minutes_now + 120):
        my_predbat.rate_import[minute] = default_rate
        my_predbat.rate_export[minute] = export_rate
    for start, end, rate in profile:
        for minute in range(start, end):
            my_predbat.rate_import[minute] = rate
    my_predbat.rate_export_min = export_rate
    my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_scan_export(my_predbat.rate_export, print=False)


def check_slot_invariants(test_name, slots):
    """
    Check the invariants all iBoost plans must hold: sorted by time, no duplicate slot starts
    """
    failed = False
    starts = [slot["start"] for slot in slots]
    if starts != sorted(starts):
        print("ERROR: {} iBoost slots are not sorted by time: {}".format(test_name, slots))
        failed = True
    if len(starts) != len(set(starts)):
        print("ERROR: {} iBoost plan contains duplicate slot starts: {}".format(test_name, slots))
        failed = True
    return failed


def run_iboost_smart_test(test_name, my_predbat, today=0, max_energy=1, max_power=1, min_length=0, expect_cost=0, expect_kwh=0, expect_time=0, expect_first_average=None):
    """
    Run a single iBoost smart planner test case and check the resulting plan totals
    """
    failed = False
    print("**** Running Test: {} ****".format(test_name))

    my_predbat.iboost_smart = True
    my_predbat.iboost_slots = []
    my_predbat.iboost_today = today
    my_predbat.iboost_max_energy = max_energy
    my_predbat.iboost_max_power = max_power / 60
    my_predbat.iboost_smart_min_length = min_length

    slots = my_predbat.plan_iboost_smart()
    total_kwh = 0
    total_cost = 0
    total_time = 0
    for slot in slots:
        total_kwh += slot["kwh"]
        total_cost += slot["cost"]
        total_time += slot["end"] - slot["start"]
    if total_time != expect_time:
        print("ERROR: Iboost total time should be {} got {}".format(expect_time, total_time))
        print(slots)
        failed = True
    if total_kwh != expect_kwh:
        print("ERROR: Iboost total kwh should be {} got {}".format(expect_kwh, total_kwh))
        print(slots)
        failed = True
    if total_cost != expect_cost:
        print(slots)
        print("ERROR: Iboost total cost should be {} got {}".format(expect_cost, total_cost))
        failed = True
    if expect_first_average is not None and (not slots or slots[0]["average"] != expect_first_average):
        print("ERROR: Iboost first slot average should be {} got {}".format(expect_first_average, slots))
        failed = True
    failed |= check_slot_invariants(test_name, slots)

    my_predbat.iboost_smart = False
    my_predbat.iboost_slots = []
    my_predbat.iboost_today = 0

    return failed


def run_iboost_smart_tests(my_predbat):
    """
    Test for Iboost smart
    """
    failed = False
    reset_inverter(my_predbat)

    import_rate = 10.0
    export_rate = 5.0
    reset_rates2(my_predbat, import_rate, export_rate)
    my_predbat.low_rates, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False)

    failed |= run_iboost_smart_test("iboost1", my_predbat, today=0, max_energy=5, max_power=1, min_length=0, expect_cost=import_rate * 5 * 2, expect_kwh=5 * 2, expect_time=5 * 2 * 60)
    failed |= run_iboost_smart_test("iboost2", my_predbat, today=4.9, max_energy=5, max_power=1, min_length=0, expect_cost=import_rate * (0.1 + 5), expect_kwh=(0.1 + 5), expect_time=10 + 5 * 60)
    failed |= run_iboost_smart_test("iboost3", my_predbat, today=4.95, max_energy=5, max_power=1, min_length=0, expect_cost=import_rate * (0.05 + 5), expect_kwh=(0.05 + 5), expect_time=5 + 5 * 60)

    # Non-flat profile with 60-minute windows: the window average must cover every interval in the
    # window (5.5 = mean of 5 and 6, not the first interval's 5) and overlapping windows must not
    # double-book a slot start (window 1410-1470 and window 1440-1500 both cover minute 1440).
    set_rate_profile(my_predbat, [(840, 870, 5.0), (870, 900, 6.0)], default_rate=20.0)
    failed |= run_iboost_smart_test("iboost_window_pricing", my_predbat, today=0, max_energy=1, max_power=1, min_length=60, expect_cost=2.75 + 2.75 + 10.0 + 10.0, expect_kwh=2.0, expect_time=120, expect_first_average=5.5)

    return failed


def make_forecast_attribute(my_predbat, points):
    """
    Build an iboost_forecast sensor attribute (dict of ISO timestamp -> cumulative kWh) from a
    list of (minute, kwh) points, with timestamps relative to midnight today
    """
    return {(my_predbat.midnight_utc + timedelta(minutes=minute)).isoformat(): kwh for minute, kwh in points}


def run_iboost_fetch_test(test_name, my_predbat, config, states, expect_demand=None, expect_total=None, expect_empty=False):
    """
    Run fetch_iboost_forecast() against mocked sensor state and check the per-interval demand

    config is merged into my_predbat.args (and removed afterwards); states is a dict of
    entity_id -> attributes dict pushed into the mock HA interface. expect_demand is a dict of
    interval start -> kWh checked to within 0.05 kWh; unlisted intervals must be (close to) zero.
    """
    failed = False
    print("**** Running Test: {} ****".format(test_name))

    for key, value in config.items():
        my_predbat.args[key] = value
    for entity_id, attributes in states.items():
        my_predbat.ha_interface.set_state(entity_id, "ok", attributes=attributes)

    demand = my_predbat.fetch_iboost_forecast()

    if expect_empty:
        if demand != {}:
            print("ERROR: {} expected an empty demand forecast, got {}".format(test_name, demand))
            failed = True
    else:
        total = sum(demand.values())
        if expect_total is not None and abs(total - expect_total) > 0.05:
            print("ERROR: {} expected total demand {} kWh got {} - demand {}".format(test_name, expect_total, total, demand))
            failed = True
        if expect_demand is not None:
            for minute, kwh in demand.items():
                if abs(kwh - expect_demand.get(minute, 0.0)) > 0.05:
                    print("ERROR: {} interval {} expected {} kWh got {} - demand {}".format(test_name, minute, expect_demand.get(minute, 0.0), kwh, demand))
                    failed = True

    for key in config:
        del my_predbat.args[key]
    for entity_id in states:
        del my_predbat.ha_interface.dummy_items[entity_id]

    return failed


def run_iboost_forecast_plan_test(test_name, my_predbat, forecast, tank_soc_percent=None, capacity=10.0, reserve=0.0, today=0, max_energy=6.0, max_power=2, fill_rate_threshold=-99.0, expect_slots=None):
    """
    Run a single iBoost forecast planner test case and compare the exact slots produced

    forecast is a dict of interval start (absolute minute) -> demand kWh; max_power is in kW.
    expect_slots is the exact expected plan (list of slot dicts).
    """
    failed = False
    print("**** Running Test: {} ****".format(test_name))

    my_predbat.iboost_smart = True
    my_predbat.iboost_today = today
    my_predbat.iboost_max_energy = max_energy
    my_predbat.iboost_max_power = max_power / 60
    my_predbat.iboost_smart_min_length = 30
    my_predbat.iboost_tank_capacity = capacity
    my_predbat.iboost_tank_reserve = reserve
    my_predbat.iboost_tank_soc_percent = tank_soc_percent
    my_predbat.iboost_fill_rate_threshold = fill_rate_threshold
    my_predbat.iboost_forecast = forecast

    slots = my_predbat.plan_iboost_smart()

    if slots != expect_slots:
        print("ERROR: {} expected slots {} got {}".format(test_name, expect_slots, slots))
        failed = True
    failed |= check_slot_invariants(test_name, slots)

    my_predbat.iboost_forecast = {}
    my_predbat.iboost_tank_soc_percent = None
    my_predbat.iboost_fill_rate_threshold = -99.0
    my_predbat.iboost_smart = False
    my_predbat.iboost_today = 0

    return failed


def run_iboost_forecast_tests(my_predbat):
    """
    Tests for the iBoost demand forecast planner and its forecast ingestion
    """
    failed = False
    reset_inverter(my_predbat)

    # The cumulative test series: flat until a 1 kWh draw over 13:00-13:30, a dip (sensor glitch,
    # must clamp to zero not negative demand) over 14:00-14:30, then a 1 kWh draw over 14:30-15:00.
    forecast_points = [(780, 0.0), (810, 1.0), (840, 1.0), (870, 0.5), (900, 1.5)]
    forecast_attribute = make_forecast_attribute(my_predbat, forecast_points)

    failed |= run_iboost_fetch_test(
        "iboost_fetch_basic",
        my_predbat,
        {"iboost_forecast": ["sensor.hot_water_demand$results"]},
        {"sensor.hot_water_demand": {"results": forecast_attribute}},
        expect_demand={780: 1.0, 870: 1.0},
        expect_total=2.0,
    )
    failed |= run_iboost_fetch_test(
        "iboost_fetch_scaling",
        my_predbat,
        {"iboost_forecast": ["sensor.hot_water_demand$results"], "iboost_forecast_scaling": 0.5},
        {"sensor.hot_water_demand": {"results": forecast_attribute}},
        expect_demand={780: 0.5, 870: 0.5},
        expect_total=1.0,
    )
    failed |= run_iboost_fetch_test(
        "iboost_fetch_unconfigured",
        my_predbat,
        {},
        {},
        expect_empty=True,
    )
    failed |= run_iboost_fetch_test(
        "iboost_fetch_missing_sensor",
        my_predbat,
        {"iboost_forecast": ["sensor.does_not_exist$results"]},
        {},
        expect_empty=True,
    )
    failed |= run_iboost_fetch_test(
        "iboost_fetch_stale",
        my_predbat,
        {"iboost_forecast": ["sensor.hot_water_demand$results"]},
        {"sensor.hot_water_demand": {"results": make_forecast_attribute(my_predbat, [(100, 0.0), (200, 1.0)])}},
        expect_empty=True,
    )

    # A draw at 14:00 with a cheap window before it and an even cheaper one after it: the energy
    # must be booked before the draw, never in the cheaper slot after it
    set_rate_profile(my_predbat, [(780, 810, 5.0), (900, 930, 2.0)], default_rate=20.0)
    failed |= run_iboost_forecast_plan_test("iboost_plan_after_cheap", my_predbat, {840: 1.0}, expect_slots=[{"start": 780, "end": 810, "kwh": 1.0, "average": 5.0, "cost": 5.0}])

    # A draw before any cheap slot books the cheapest slot preceding it even though cheaper
    # slots exist later
    set_rate_profile(my_predbat, [(780, 810, 5.0)], default_rate=20.0)
    failed |= run_iboost_forecast_plan_test("iboost_plan_draw_before_cheap", my_predbat, {750: 1.0}, expect_slots=[{"start": 720, "end": 750, "kwh": 1.0, "average": 20.0, "cost": 20.0}])

    # A full tank already covers the whole forecast: no slots are booked
    failed |= run_iboost_forecast_plan_test("iboost_plan_tank_full", my_predbat, {840: 1.0, 900: 2.0}, tank_soc_percent=100.0, expect_slots=[])

    # The reserve is held before the draw: only the shortfall over (reserve + demand) is booked
    failed |= run_iboost_forecast_plan_test("iboost_plan_reserve", my_predbat, {840: 1.5}, tank_soc_percent=20.0, reserve=1.0, expect_slots=[{"start": 780, "end": 800, "kwh": 0.5, "average": 5.0, "cost": 2.5}])

    # Two draws share one cheap interval up to the capacity: the level trajectory peaks exactly
    # at the 1 kWh capacity and never above it
    set_rate_profile(my_predbat, [(780, 810, 5.0), (810, 840, 6.0)], default_rate=20.0)
    failed |= run_iboost_forecast_plan_test("iboost_plan_capacity", my_predbat, {840: 0.5, 900: 0.5}, capacity=1.0, expect_slots=[{"start": 780, "end": 810, "kwh": 1.0, "average": 5.0, "cost": 5.0}])

    # A large cheap window cannot overfill the tank: only the headroom over the stored energy is
    # booked even though the slot could take far more
    set_rate_profile(my_predbat, [(780, 810, 5.0)], default_rate=20.0)
    failed |= run_iboost_forecast_plan_test("iboost_plan_headroom", my_predbat, {840: 0.8}, tank_soc_percent=50.0, capacity=1.0, expect_slots=[{"start": 780, "end": 790, "kwh": 0.3, "average": 5.0, "cost": 1.5}])

    # The calendar-day cap holds alongside the forecast: day 0 has only 0.2 kWh left so the rest
    # of that draw goes uncovered (warning, no exception) while day 1 demand is still planned
    set_rate_profile(my_predbat, [(780, 810, 5.0), (810, 840, 6.0)], default_rate=20.0)
    failed |= run_iboost_forecast_plan_test(
        "iboost_plan_day_cap",
        my_predbat,
        {840: 1.0, 1500: 0.4},
        today=0.3,
        max_energy=0.5,
        expect_slots=[{"start": 780, "end": 790, "kwh": 0.2, "average": 5.0, "cost": 1.0}, {"start": 1440, "end": 1455, "kwh": 0.4, "average": 20.0, "cost": 8.0}],
    )

    # A draw in the very first interval has no earlier slot to book: warn and keep planning the
    # remaining draws
    set_rate_profile(my_predbat, [(780, 810, 5.0)], default_rate=20.0)
    failed |= run_iboost_forecast_plan_test("iboost_plan_uncovered_first", my_predbat, {720: 1.0, 840: 1.0}, expect_slots=[{"start": 780, "end": 810, "kwh": 1.0, "average": 5.0, "cost": 5.0}])

    # Fill threshold: a negative-rate slot qualifies for filling, so on top of the 0.5 kWh the
    # draw needs, the remaining headroom is bought in the -2p slot up to the element power
    set_rate_profile(my_predbat, [(780, 810, -2.0)], default_rate=20.0)
    failed |= run_iboost_forecast_plan_test("iboost_plan_fill", my_predbat, {840: 0.5}, capacity=2.0, fill_rate_threshold=0.0, expect_slots=[{"start": 780, "end": 810, "kwh": 1.0, "average": -2.0, "cost": -2.0}])

    # Fill threshold at or above the ambient rate fills from the start of the plan; the earlier
    # fills carry forward to the draw, the trajectory is re-verified and the boosts booked in the
    # 5p/6p slots are trimmed so the level never exceeds the 1 kWh capacity anywhere
    set_rate_profile(my_predbat, [(780, 810, 5.0), (840, 870, 6.0)], default_rate=20.0)
    failed |= run_iboost_forecast_plan_test(
        "iboost_plan_fill_trim",
        my_predbat,
        {900: 1.0},
        capacity=1.0,
        max_power=1,
        fill_rate_threshold=25.0,
        expect_slots=[
            {"start": 720, "end": 750, "kwh": 0.5, "average": 20.0, "cost": 10.0},
            {"start": 750, "end": 780, "kwh": 0.5, "average": 20.0, "cost": 10.0},
            {"start": 900, "end": 930, "kwh": 0.5, "average": 20.0, "cost": 10.0},
            {"start": 930, "end": 960, "kwh": 0.5, "average": 20.0, "cost": 10.0},
        ],
    )

    # Regression: with no forecast loaded the legacy planner runs and produces identical output
    # before and after a forecast plan has been made, and the forecast plan itself differs
    set_rate_profile(my_predbat, [(780, 810, 5.0)], default_rate=20.0)
    my_predbat.iboost_smart = True
    my_predbat.iboost_today = 0
    my_predbat.iboost_max_energy = 1
    my_predbat.iboost_max_power = 1 / 60
    my_predbat.iboost_smart_min_length = 0
    my_predbat.iboost_forecast = {}
    legacy_before = my_predbat.plan_iboost_smart()
    my_predbat.iboost_tank_capacity = 10.0
    my_predbat.iboost_tank_reserve = 0.0
    my_predbat.iboost_tank_soc_percent = None
    my_predbat.iboost_forecast = {840: 1.0}
    forecast_plan = my_predbat.plan_iboost_smart()
    my_predbat.iboost_forecast = {}
    legacy_after = my_predbat.plan_iboost_smart()
    print("**** Running Test: iboost_plan_regression ****")
    if legacy_before != legacy_after:
        print("ERROR: iboost_plan_regression legacy plan changed after running the forecast planner: {} vs {}".format(legacy_before, legacy_after))
        failed = True
    if not legacy_before:
        print("ERROR: iboost_plan_regression legacy plan is unexpectedly empty")
        failed = True
    if forecast_plan == legacy_before:
        print("ERROR: iboost_plan_regression forecast plan did not diverge from the legacy plan: {}".format(forecast_plan))
        failed = True
    my_predbat.iboost_smart = False
    my_predbat.iboost_today = 0

    return failed
