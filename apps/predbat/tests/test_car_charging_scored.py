# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for scoring the car's charging windows against the forecast.

With car_charging_from_battery the home battery serves the car, so a window's import rate is not what
charging in it costs. plan_car_charging_scored prices each candidate window by running the forecast
with the car's load in it, which is what these tests exercise - both that it is only used when it can
say something the import rate cannot, and that it then picks a different window than the price sort.
"""

from prediction import Prediction
from tests.test_infra import reset_rates, reset_inverter


def setup_scored_car(my_predbat, forecast_hours=24, load_per_step=0.05, import_rate=100.0, export_rate=5.0):
    """Put my_predbat into a state where plan_car_charging_scored can run, and return the load dict."""
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)

    my_predbat.forecast_minutes = forecast_hours * 60
    my_predbat.end_record = my_predbat.forecast_minutes

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes + my_predbat.plan_interval_minutes, 5):
        pv_step[minute] = 0.0
        load_step[minute] = load_per_step

    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    reset_rates(my_predbat, import_rate, export_rate)
    my_predbat.rate_import_base = my_predbat.rate_import.copy()
    my_predbat.rate_export_base = my_predbat.rate_export.copy()

    # A plan from a previous cycle for the scoring to measure against - no windows is still a plan
    my_predbat.charge_limit_best = [0.0]
    my_predbat.charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": import_rate}]
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []

    my_predbat.num_cars = 1
    my_predbat.car_charging_from_battery = True
    my_predbat.car_energy_reported_load = True
    my_predbat.car_charging_in_load_history = False
    my_predbat.car_charging_battery_size = [50.0]
    my_predbat.car_charging_limit = [2.0]
    my_predbat.car_charging_soc = [0.0]
    my_predbat.car_charging_soc_next = [None]
    my_predbat.car_charging_rate = [4.0]
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_plan_max_price = [0]
    my_predbat.car_charging_plan_smart = [True]
    my_predbat.car_charging_plan_time = ["23:00:00"]
    my_predbat.car_charging_now = [False]
    my_predbat.car_charging_slots = [[]]
    return load_step


def run_scored_gate_tests(my_predbat):
    """car_scored_charging_enabled must only turn scoring on where it can say something useful."""
    failed = False
    print("**** Running Test: car_scored_charging_enabled gates ****")

    setup_scored_car(my_predbat)

    if not my_predbat.car_scored_charging_enabled(0):
        print("ERROR: scoring should be enabled with car_charging_from_battery, smart and reported load")
        failed = True

    my_predbat.car_charging_from_battery = False
    if my_predbat.car_scored_charging_enabled(0):
        print("ERROR: scoring must be off when the battery may not feed the car")
        failed = True
    my_predbat.car_charging_from_battery = True

    my_predbat.car_charging_plan_smart = [False]
    if my_predbat.car_scored_charging_enabled(0):
        print("ERROR: scoring must be off when smart charging is off")
        failed = True
    my_predbat.car_charging_plan_smart = [True]

    my_predbat.car_energy_reported_load = False
    if my_predbat.car_scored_charging_enabled(0):
        print("ERROR: scoring must be off when the car energy is not reported as load")
        failed = True
    my_predbat.car_energy_reported_load = True

    my_predbat.car_charging_in_load_history = True
    if my_predbat.car_scored_charging_enabled(0):
        print("ERROR: scoring must be off when the car energy is already in the load history")
        failed = True
    my_predbat.car_charging_in_load_history = False

    my_predbat.charge_limit_best = []
    if my_predbat.car_scored_charging_enabled(0):
        print("ERROR: scoring must be off with no previous plan to score against")
        failed = True

    return failed


def run_slot_extra_load_tests(my_predbat):
    """car_slot_extra_load must place exactly the slot's kWh on the PREDICT_STEP grid."""
    failed = False
    print("**** Running Test: car_slot_extra_load ****")

    setup_scored_car(my_predbat)
    now = my_predbat.minutes_now

    # Aligned slot: one hour at 4kW
    slot = {"start": now + 60, "end": now + 120, "kwh": 4.0}
    extra = my_predbat.car_slot_extra_load(slot)
    total = sum(extra.values())
    if abs(total - 4.0) > 0.0001:
        print("ERROR: aligned slot should place 4.0kWh, got {}".format(total))
        failed = True
    if min(extra.keys()) != 60 or max(extra.keys()) != 115:
        print("ERROR: aligned slot should cover relative minutes 60-115, got {}-{}".format(min(extra.keys()), max(extra.keys())))
        failed = True

    # Unaligned slot: still exactly the slot's energy, clipped into the part-covered steps
    slot = {"start": now + 62, "end": now + 93, "kwh": 2.0}
    extra = my_predbat.car_slot_extra_load(slot)
    total = sum(extra.values())
    if abs(total - 2.0) > 0.0001:
        print("ERROR: unaligned slot should place 2.0kWh, got {}".format(total))
        failed = True

    # Nothing to place
    if my_predbat.car_slot_extra_load({"start": now, "end": now, "kwh": 1.0}):
        print("ERROR: a zero length slot should place no load")
        failed = True
    if my_predbat.car_slot_extra_load({"start": now, "end": now + 60, "kwh": 0.0}):
        print("ERROR: a zero energy slot should place no load")
        failed = True

    return failed


def set_ready_time(my_predbat, minutes_out):
    """Point the car's plan time at minutes_out from now and return the absolute ready minute."""
    ready = my_predbat.minutes_now + minutes_out
    my_predbat.car_charging_plan_time = ["{:02d}:{:02d}:00".format((ready // 60) % 24, ready % 60)]
    return ready


def hourly_windows(my_predbat, hours, default_rate):
    """Build the hourly candidate windows plan_car_charging is normally handed."""
    windows = []
    for hour in range(0, hours):
        start = my_predbat.minutes_now + hour * 60
        windows.append({"start": start, "end": start + 60, "average": my_predbat.rate_import.get(start, default_rate)})
    return windows


def run_scored_prefers_battery_tests(my_predbat):
    """The battery wins when it is worth less than the cheapest import the car could reach.

    This is the shape that made the difference in practice: every hour before the car has to be ready is
    expensive, and the cheap rate only arrives afterwards. The car cannot wait for it, but the battery
    can be refilled at it, so a kWh out of the battery costs the cheap rate while the car's own options
    all cost the expensive one. A price sort cannot see that - it only compares the hours the car can use.
    """
    failed = False
    print("**** Running Test: scored plan prefers the battery to an expensive import hour ****")

    setup_scored_car(my_predbat)
    now = my_predbat.minutes_now
    ready = set_ready_time(my_predbat, 12 * 60)

    # Expensive until the car is due, with one slightly better hour for the price sort to find, then cheap
    for minute in range(now, now + my_predbat.forecast_minutes):
        my_predbat.rate_import[minute] = 150.0 if minute < ready else 20.0
    cheap_start = now + 8 * 60
    for minute in range(cheap_start, cheap_start + 60):
        my_predbat.rate_import[minute] = 120.0
    my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_import_base = my_predbat.rate_import.copy()

    # Battery full and large enough that the car never forces an import
    my_predbat.soc_max = 50.0
    my_predbat.soc_kw = 50.0
    my_predbat.battery_rate_max_discharge = 5 / 60.0
    my_predbat.battery_rate_max_charge = 5 / 60.0

    low_rates = hourly_windows(my_predbat, 12, 150.0)

    scored = my_predbat.plan_car_charging(0, low_rates)
    if not scored:
        print("ERROR: scored plan produced no slots")
        return True

    # The same problem with the battery locked out is what the import rate alone can see
    my_predbat.car_charging_from_battery = False
    my_predbat.car_charging_soc = [0.0]
    priced = my_predbat.plan_car_charging(0, low_rates)
    if not priced:
        print("ERROR: price sorted plan produced no slots")
        return True

    if priced[0]["start"] != cheap_start:
        print("ERROR: price sorted plan should take the {} hour, got {}".format(cheap_start, priced[0]["start"]))
        failed = True

    if scored[0]["source"] != "battery":
        print("ERROR: scored slot should be sourced from the battery, got {} at {}{}".format(scored[0]["source"], scored[0]["effective"], " "))
        failed = True

    if scored[0]["effective"] >= priced[0]["average"]:
        print("ERROR: scored slot should beat the cheapest import hour {}, got {}".format(priced[0]["average"], scored[0]["effective"]))
        failed = True

    total_kwh = sum(slot["kwh"] for slot in scored)
    if abs(total_kwh - 2.0) > 0.01:
        print("ERROR: scored plan should still deliver 2.0kWh, got {}".format(total_kwh))
        failed = True

    for slot in scored:
        if slot["end"] > ready:
            print("ERROR: scored slot {}-{} runs past the ready time {}".format(slot["start"], slot["end"], ready))
            failed = True

    return failed


def run_scored_respects_export_tests(my_predbat):
    """The car waits rather than eat a forced export the battery cannot outrun.

    The battery is at its discharge limit throughout a short, well paid export window, so a kWh the car
    takes there is one that never gets sold at that price. Import inside the window is expensive too,
    which is the pairing that made this bite in practice. Charging after it is what the car should do.
    """
    failed = False
    print("**** Running Test: scored plan leaves a saturated export window alone ****")

    setup_scored_car(my_predbat)
    now = my_predbat.minutes_now
    ready = set_ready_time(my_predbat, 12 * 60)

    export_start = now + 60
    export_end = export_start + 120
    for minute in range(now, now + my_predbat.forecast_minutes):
        in_window = export_start <= minute < export_end
        my_predbat.rate_export[minute] = 140.0 if in_window else 20.0
        my_predbat.rate_import[minute] = 260.0 if in_window else (150.0 if minute < ready else 20.0)
    my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_scan_export(my_predbat.rate_export, print=False)
    my_predbat.rate_import_base = my_predbat.rate_import.copy()
    my_predbat.rate_export_base = my_predbat.rate_export.copy()

    my_predbat.soc_max = 50.0
    my_predbat.soc_kw = 50.0
    # Discharge limit below what the window would take on its own, so the battery is saturated in it
    my_predbat.battery_rate_max_discharge = 10 / 60.0
    my_predbat.battery_rate_max_charge = 10 / 60.0

    # The previous cycle's plan sells hard into that window
    my_predbat.export_window_best = [{"start": export_start, "end": export_end, "average": 140.0}]
    my_predbat.export_limits_best = [10.0]

    low_rates = hourly_windows(my_predbat, 12, 150.0)

    plan = my_predbat.plan_car_charging(0, low_rates)
    if not plan:
        print("ERROR: scored plan produced no slots")
        return True

    for slot in plan:
        if slot["start"] < export_end and slot["end"] > export_start:
            print("ERROR: scored slot {}-{} overlaps the export window {}-{}".format(slot["start"], slot["end"], export_start, export_end))
            failed = True

    total_kwh = sum(slot["kwh"] for slot in plan)
    if abs(total_kwh - 2.0) > 0.01:
        print("ERROR: scored plan should still deliver 2.0kWh, got {}".format(total_kwh))
        failed = True

    # Without the export window in the way the same hours are fair game, so the test is not vacuous
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.car_charging_soc = [0.0]
    for minute in range(export_start, export_end):
        my_predbat.rate_import[minute] = 150.0
    my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_import_base = my_predbat.rate_import.copy()
    low_rates = hourly_windows(my_predbat, 12, 150.0)
    free = my_predbat.plan_car_charging(0, low_rates)
    if not free:
        print("ERROR: control plan produced no slots")
        failed = True
    elif free[0]["start"] >= export_end:
        print("ERROR: control plan avoided the window too, so the export window was not what moved the slot")
        failed = True

    return failed


def run_car_charging_scored_tests(my_predbat):
    """Run every scored car charging test.

    setup_scored_car rebinds a lot of my_predbat - the forecast horizon, the load and PV steps, the plan
    and the whole car configuration - and my_predbat is shared with every test after this one. Snapshot
    the attribute bindings and put them back, or a later test inherits a 24 hour horizon and step dicts
    sized to match it.
    """
    failed = False
    saved = dict(my_predbat.__dict__)
    try:
        failed |= run_scored_gate_tests(my_predbat)
        failed |= run_slot_extra_load_tests(my_predbat)
        failed |= run_scored_prefers_battery_tests(my_predbat)
        failed |= run_scored_respects_export_tests(my_predbat)
    finally:
        my_predbat.__dict__.clear()
        my_predbat.__dict__.update(saved)
    if not failed:
        print("**** Car charging scored tests passed ****")
    return failed
