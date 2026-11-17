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
Today's cost for a car charger wired outside the CT clamp (car_energy_reported_load off).

The import sensor then misses the car, but the supply meter bills it, so today_cost() adds the car's energy measured
by its own sensor as import. The car sits between the clamp and the meter, so what the clamp counts as export goes
into the car first and only the rest is imported, as the prediction treats it. On Octopus Intelligent the import rate
is the one the completed dispatches gave each minute, with the peak rate beyond the cap.
"""

import copy

from tests.test_iog_cap_cost_today import EXTRA_FIELDS, RATE_PEAK, _car_minutes, _incrementing, _setup_friday
from tests.test_octopus_dispatch_hold import STATE_FIELDS, _check

RATE_FLAT = 24.5
RATE_EXPORT = 15.0
CLAMP_FIELDS = ("car_energy_reported_load", "import_today", "export_today", "car_charging_energy")


def _cost(my_predbat, import_minutes, export_minutes, car_minutes):
    """
    today_cost() for the energy in each minute of import, export and car charging, the house load being the import.
    """
    minutes_now = my_predbat.minutes_now
    import_today = _incrementing(minutes_now, import_minutes)
    export_today = _incrementing(minutes_now, export_minutes)
    car_today = _incrementing(minutes_now, car_minutes)
    cost, _ = my_predbat.today_cost(import_today, export_today, car_today, import_today, save=False)
    return cost


def _setup_flat(my_predbat, minutes_now):
    """
    A car on a flat-rate tariff with no Octopus dispatches, at minutes_now past midnight.
    """
    my_predbat.num_cars = 1
    my_predbat.octopus_slots = [[]]
    my_predbat.minutes_now = minutes_now
    my_predbat.metric_standing_charge = 0
    my_predbat.carbon_enable = False
    my_predbat.soc_kwh_history = {}
    my_predbat.rate_max_base = RATE_FLAT
    my_predbat.rate_import = {minute: RATE_FLAT for minute in range(-24 * 60, 2 * 24 * 60)}
    my_predbat.rate_export = {minute: RATE_EXPORT for minute in range(-24 * 60, 2 * 24 * 60)}


def _run(my_predbat):
    """
    Run the scenarios, returning True when any check failed.
    """
    failed = False
    house = {minute: 0.01 for minute in range(0, 600)}
    car = {minute: 0.12 for minute in range(120, 180)}  # 7.2 kWh from 02:00 to 03:00

    print("Test 1: a flat-rate car outside the clamp is costed at the import rate")
    _setup_flat(my_predbat, 600)
    my_predbat.car_energy_reported_load = False
    cost = _cost(my_predbat, house, {}, car)
    expected = (sum(house.values()) + sum(car.values())) * RATE_FLAT
    failed |= _check("t1 outside the clamp", abs(cost - expected) < 0.5, "got {:.2f}p expected {:.2f}p".format(cost, expected))

    print("Test 2: inside the clamp the import sensor already has the car, so it is not added again")
    my_predbat.car_energy_reported_load = True
    cost = _cost(my_predbat, house, {}, car)
    expected = sum(house.values()) * RATE_FLAT
    failed |= _check("t2 inside the clamp", abs(cost - expected) < 0.5, "got {:.2f}p expected {:.2f}p".format(cost, expected))

    print("Test 3: outside the clamp, what the clamp counts as export goes into the car first")
    # 0.05 kWh a minute of export by the clamp while the car draws 0.12: the meter exports nothing and imports 0.07
    export = {minute: 0.05 for minute in range(120, 180)}
    my_predbat.car_energy_reported_load = False
    cost = _cost(my_predbat, house, export, car)
    expected = sum(house.values()) * RATE_FLAT + 60 * 0.07 * RATE_FLAT
    failed |= _check("t3 export into the car", abs(cost - expected) < 0.5, "got {:.2f}p expected {:.2f}p".format(cost, expected))
    # And with more export than the car draws, the meter exports the rest
    export = {minute: 0.2 for minute in range(120, 180)}
    cost = _cost(my_predbat, house, export, car)
    expected = sum(house.values()) * RATE_FLAT - 60 * (0.2 - 0.12) * RATE_EXPORT
    failed |= _check("t3 export beyond the car", abs(cost - expected) < 0.5, "got {:.2f}p expected {:.2f}p".format(cost, expected))

    print("Test 4: on Octopus Intelligent the car outside the clamp pays the rate its completed dispatches gave each minute")
    car = _setup_friday(my_predbat, 1435)
    my_predbat.car_energy_reported_load = False
    my_predbat.rate_export = {}
    # The rates as Predbat builds them: the day rate, with the dispatches inside the cap at the off-peak rate
    rates = {minute: RATE_PEAK for minute in range(-2 * 24 * 60, 2 * 24 * 60)}
    my_predbat.io_adjusted = {}
    my_predbat.rate_import = my_predbat.rate_add_io_slots(0, rates, my_predbat.octopus_slots[0])
    # The car also charged at 18:00-18:30, with no dispatch
    car.update({minute: 0.12 for minute in range(1080, 1110)})
    cost = _cost(my_predbat, {}, {}, car)
    in_cap = sum(kwh for minute, kwh in car.items() if minute in _car_minutes() and minute < 1410)
    at_peak = sum(kwh for minute, kwh in car.items() if minute not in _car_minutes() or minute >= 1410)
    expected = in_cap * my_predbat.rate_min_base + at_peak * RATE_PEAK
    failed |= _check("t4 dispatch-priced car", abs(cost - expected) < 0.5, "got {:.2f}p expected {:.2f}p (in cap {:.2f} kWh, at peak {:.2f} kWh)".format(cost, expected, in_cap, at_peak))
    failed |= _check("t4 dispatch minutes are off-peak", my_predbat.rate_import[1300] == my_predbat.rate_min_base and my_predbat.rate_import[1090] == RATE_PEAK, "rates {} {}".format(my_predbat.rate_import[1300], my_predbat.rate_import[1090]))

    print("Test 5: the History rows' kWh are what the meter saw, the car outside the clamp included")
    _setup_flat(my_predbat, 600)
    export = {minute: 0.05 for minute in range(150, 180)}
    car = {minute: 0.12 for minute in range(120, 180)}
    my_predbat.import_today = _incrementing(600, house)
    my_predbat.export_today = _incrementing(600, export)
    my_predbat.car_charging_energy = _incrementing(600, car)
    my_predbat.car_energy_reported_load = False
    import_rows, export_rows = my_predbat.history_row_meter_kwh(600, 600)
    # 02:00-02:30: the house's 0.3 kWh and all the car's 3.6; 02:30-03:00: the car takes the clamp's export, importing 0.07 a minute
    failed |= _check("t5 car row import", abs(import_rows[120] - (0.3 + 3.6)) < 0.001 and abs(import_rows[150] - (0.3 + 30 * 0.07)) < 0.001, "rows {} {}".format(import_rows[120], import_rows[150]))
    failed |= _check("t5 export into the car is not exported", abs(export_rows[150]) < 0.001, "row {}".format(export_rows[150]))
    failed |= _check("t5 house-only row", abs(import_rows[0] - 0.3) < 0.001, "row {}".format(import_rows[0]))
    my_predbat.car_energy_reported_load = True
    import_rows, export_rows = my_predbat.history_row_meter_kwh(600, 600)
    failed |= _check("t5 inside the clamp the sensors are taken as they are", abs(import_rows[120] - 0.3) < 0.001 and abs(export_rows[150] - 1.5) < 0.001, "rows {} {}".format(import_rows[120], export_rows[150]))
    my_predbat.export_today = {}
    _, export_rows = my_predbat.history_row_meter_kwh(600, 600)
    failed |= _check("t5 no export sensor, no export rows", export_rows is None, "rows {}".format(export_rows))

    return failed


def test_today_cost_car_clamp(my_predbat):
    """
    A car outside the CT clamp is in today's cost.
    """
    print("*** Running test: today cost car outside the CT clamp")
    fields = STATE_FIELDS + EXTRA_FIELDS + CLAMP_FIELDS
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
    print("*** Today cost car outside the CT clamp test {}".format("FAILED" if failed else "PASSED"))
    return failed
