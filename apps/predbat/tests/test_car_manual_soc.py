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
Manual car SoC tracking (car_charging_manual_soc).

- A car with no car_charging_soc sensor is tracked manually. The switch only forces manual tracking
  for a car that has one, and the kWh entity is there for every car whatever the switch says.
- A car with car_charging_planned configured has its manual SoC reset to zero when it is unplugged.
- A car with a car_charging_energy sensor has its manual SoC raised by the energy the charger's
  history shows, rather than by the plan's predicted charge.

Both work from when the manual SoC kWh entity last changed, so nothing is lost over a restart.
"""
import copy
from datetime import timedelta

ENERGY_SENSOR = "sensor.car_manual_soc_test_energy"
PLUGGED_SENSOR = "binary_sensor.car_manual_soc_test_plugged"
PLANNED_SENSOR = "binary_sensor.car_manual_soc_test_planned"
SOC_SENSOR = "sensor.car_manual_soc_test_soc"

STATE_FIELDS = (
    "num_cars",
    "car_charging_manual_soc",
    "car_charging_manual_soc_energy",
    "car_manual_soc_written",
    "car_charging_energy",
    "car_charging_soc",
    "car_charging_soc_next",
    "car_charging_limit",
    "car_charging_loss",
    "car_charging_battery_size",
    "car_charging_planned",
    "car_charging_energy_scale",
)
SAVED_ARGS = ("car_charging_soc", "car_charging_planned", "car_charging_plugged", "car_charging_energy")
KWH_ENTITY = "input_number.predbat_car_charging_manual_soc_kwh"
SAVED_CONFIG = ("car_charging_manual_soc", "car_charging_manual_soc_kwh", "car_charging_manual_soc_1", "car_charging_manual_soc_kwh_1")


def _check(name, condition, detail=""):
    """
    Print an error for a failed condition and return whether it failed.
    """
    if not condition:
        print("ERROR: {} {}".format(name, detail))
        return True
    return False


def _manual_kwh(my_predbat, car_postfix=""):
    """
    The manual SoC kWh value as Predbat reads it back.
    """
    return my_predbat.get_arg("car_charging_manual_soc_kwh" + car_postfix, 0.0)


def _ago(my_predbat, minutes):
    """
    The time the given number of minutes before the fixture's now.
    """
    return my_predbat.now_utc - timedelta(minutes=minutes)


def _history(points):
    """
    A cumulative kWh series indexed by minutes ago, as minute_data() builds it, from (minutes ago, kWh charged
    in the minute before) pairs - each pair adds its energy to the total from that minute onwards.
    """
    history = {}
    for minute in range(24 * 60):
        history[minute] = sum(kwh for ago, kwh in points if ago >= minute)
    return history


def _changed(my_predbat, minutes_ago):
    """
    Make Home Assistant report the manual SoC kWh entity as last changed minutes_ago, as after a restart.
    """
    my_predbat.ha_interface.dummy_items[KWH_ENTITY] = {"state": _manual_kwh(my_predbat), "last_changed": _ago(my_predbat, minutes_ago)}
    my_predbat.car_manual_soc_written = {}


def _one_car(my_predbat, args):
    """
    A single manual SoC car, with apps.yaml holding only the given car args.
    """
    for arg in SAVED_ARGS:
        my_predbat.args.pop(arg, None)
    my_predbat.args.update(args)
    my_predbat.num_cars = 1
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_energy_scale = 1.0
    my_predbat.car_charging_battery_size = [60.0]
    my_predbat.car_charging_limit = [60.0]
    my_predbat.car_manual_soc_written = {}
    my_predbat.car_charging_energy = {}
    # Left off: with no car_charging_soc the car is manual anyway, and its kWh entity is not gated on the switch
    my_predbat.expose_config("car_charging_manual_soc", False)
    my_predbat.car_manual_soc_config()


def _run_config(my_predbat):
    """
    car_manual_soc_config() turns manual SoC on for cars with no SoC sensor.
    """
    failed = False
    my_predbat.expose_config("car_charging_manual_soc", False)
    my_predbat.expose_config("car_charging_manual_soc_1", False)

    print("Test 1: no car_charging_soc - manual SoC is on even with the switch off")
    for arg in SAVED_ARGS:
        my_predbat.args.pop(arg, None)
    my_predbat.num_cars = 1
    my_predbat.car_manual_soc_config()
    failed |= _check("t1 manual", my_predbat.car_charging_manual_soc[:1] == [True], "manual {}".format(my_predbat.car_charging_manual_soc))

    print("Test 2: car_charging_soc configured - the switch decides")
    my_predbat.args["car_charging_soc"] = SOC_SENSOR
    my_predbat.car_manual_soc_config()
    failed |= _check("t2 not manual", my_predbat.car_charging_manual_soc[:1] == [False], "manual {}".format(my_predbat.car_charging_manual_soc))
    my_predbat.expose_config("car_charging_manual_soc", True)
    my_predbat.car_manual_soc_config()
    failed |= _check("t2 switch on", my_predbat.car_charging_manual_soc[:1] == [True], "manual {}".format(my_predbat.car_charging_manual_soc))
    my_predbat.expose_config("car_charging_manual_soc", False)

    print("Test 3: a list with no entry for car 1 - car 1 only is manual")
    my_predbat.num_cars = 2
    my_predbat.args["car_charging_soc"] = [SOC_SENSOR]
    my_predbat.car_manual_soc_config()
    failed |= _check("t3 short list", my_predbat.car_charging_manual_soc[:2] == [False, True], "manual {}".format(my_predbat.car_charging_manual_soc))
    my_predbat.args["car_charging_soc"] = [SOC_SENSOR, None]
    my_predbat.car_manual_soc_config()
    failed |= _check("t3 unmatched entry", my_predbat.car_charging_manual_soc[:2] == [False, True], "manual {}".format(my_predbat.car_charging_manual_soc))

    print("Test 4: a single sensor covers car 0 only")
    my_predbat.args["car_charging_soc"] = SOC_SENSOR
    my_predbat.car_manual_soc_config()
    failed |= _check("t4 scalar", my_predbat.car_charging_manual_soc[:2] == [False, True], "manual {}".format(my_predbat.car_charging_manual_soc))

    print("Test 5: an unresolved regular expression is not a sensor")
    my_predbat.num_cars = 1
    my_predbat.args["car_charging_soc"] = "re:sensor.no_such_car"
    my_predbat.car_manual_soc_config()
    failed |= _check("t5 regex", my_predbat.car_charging_manual_soc[:1] == [True], "manual {}".format(my_predbat.car_charging_manual_soc))

    print("Test 6: car_charging_energy marks the car as energy-tracked, per car")
    my_predbat.num_cars = 2
    my_predbat.args.pop("car_charging_soc", None)
    my_predbat.args["car_charging_energy"] = [ENERGY_SENSOR]
    my_predbat.car_manual_soc_config()
    failed |= _check("t6 energy", my_predbat.car_charging_manual_soc_energy[:2] == [True, False], "energy {}".format(my_predbat.car_charging_manual_soc_energy))
    my_predbat.args.pop("car_charging_energy", None)
    my_predbat.car_manual_soc_config()
    failed |= _check("t6 no energy", my_predbat.car_charging_manual_soc_energy[:2] == [False, False], "energy {}".format(my_predbat.car_charging_manual_soc_energy))
    return failed


def _run_switch(my_predbat):
    """
    The switch only forces manual tracking - Predbat never turns it on, and the kWh entity is there without it.
    """
    failed = False
    print("Test 7: an automatically manual car leaves its switch off and keeps its kWh value")
    _one_car(my_predbat, {})
    my_predbat.expose_config("car_charging_manual_soc_kwh", 12.5)
    my_predbat.car_manual_soc_track()
    failed |= _check("t7 switch", my_predbat.get_arg("car_charging_manual_soc", False) is False, "switch {}".format(my_predbat.get_arg("car_charging_manual_soc", False)))
    failed |= _check("t7 kwh", abs(_manual_kwh(my_predbat) - 12.5) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    return failed


def _run_energy(my_predbat):
    """
    The manual SoC rises by the energy the charger's history shows since the kWh entity last changed.
    """
    failed = False
    _one_car(my_predbat, {"car_charging_energy": ENERGY_SENSOR})
    my_predbat.expose_config("car_charging_manual_soc_kwh", 10.0)
    # 1 kWh 3 hours ago, then 2 kWh over the last half hour
    my_predbat.car_charging_energy = _history([(180, 1.0), (20, 1.0), (5, 1.0)])

    print("Test 8: energy since the entity last changed is added, less charging losses - with nothing remembered, as after a restart")
    _changed(my_predbat, 60)
    my_predbat.car_charging_loss = 0.9
    my_predbat.car_manual_soc_track()
    failed |= _check("t8 added", abs(_manual_kwh(my_predbat) - 11.8) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t8 soc", abs(my_predbat.car_charging_soc[0] - 11.8) < 0.001, "soc {}".format(my_predbat.car_charging_soc))
    my_predbat.car_charging_loss = 1.0

    print("Test 9: Predbat's own write counts before Home Assistant reports it, so nothing is added twice")
    my_predbat.ha_interface.dummy_items[KWH_ENTITY] = {"state": 11.8, "last_changed": _ago(my_predbat, 60)}
    my_predbat.car_manual_soc_track()
    failed |= _check("t9 not twice", abs(_manual_kwh(my_predbat) - 11.8) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 10: a value the user entered counts from when they entered it")
    my_predbat.expose_config("car_charging_manual_soc_kwh", 20.0)
    _changed(my_predbat, 10)
    my_predbat.car_manual_soc_track()
    failed |= _check("t10 since edit", abs(_manual_kwh(my_predbat) - 21.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 11: a time older than the history counts the whole history")
    my_predbat.expose_config("car_charging_manual_soc_kwh", 20.0)
    _changed(my_predbat, 3 * 24 * 60)
    my_predbat.car_manual_soc_track()
    failed |= _check("t11 whole history", abs(_manual_kwh(my_predbat) - 23.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 12: no history adds nothing")
    my_predbat.expose_config("car_charging_manual_soc_kwh", 20.0)
    _changed(my_predbat, 60)
    my_predbat.car_charging_energy = {}
    saved_fetch = my_predbat.minute_data_import_export
    my_predbat.minute_data_import_export = lambda *args, **kwargs: {}
    try:
        my_predbat.car_manual_soc_track()
    finally:
        my_predbat.minute_data_import_export = saved_fetch
    failed |= _check("t12 nothing", abs(_manual_kwh(my_predbat) - 20.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 13: a second car fetches its own charger's history, not the summed one")
    calls = []

    def fake_fetch(days, now_utc, key, scale=1.0, required_unit=None, **kwargs):
        """
        Record the entity asked for and return 4 kWh charged in the last 10 minutes.
        """
        calls.append(key)
        return _history([(10, 4.0)])

    # The car 1 kWh entity is enabled by the num_cars setting, not the attribute
    saved_num_cars = my_predbat.args.get("num_cars", None)
    my_predbat.args["num_cars"] = 2
    my_predbat.num_cars = 2
    my_predbat.args["car_charging_energy"] = ["sensor.charger_0", ENERGY_SENSOR]
    my_predbat.car_charging_battery_size = [60.0, 60.0]
    my_predbat.car_charging_soc = [0.0, 0.0]
    my_predbat.car_manual_soc_config()
    my_predbat.expose_config("car_charging_manual_soc_kwh", 20.0)
    my_predbat.expose_config("car_charging_manual_soc_kwh_1", 5.0)
    my_predbat.car_manual_soc_written = {0: _ago(my_predbat, 60), 1: _ago(my_predbat, 60)}
    my_predbat.minute_data_import_export = fake_fetch
    try:
        my_predbat.car_manual_soc_track()
        car_1_kwh = _manual_kwh(my_predbat, "_1")
    finally:
        my_predbat.minute_data_import_export = saved_fetch
        if saved_num_cars is None:
            my_predbat.args.pop("num_cars", None)
        else:
            my_predbat.args["num_cars"] = saved_num_cars
    failed |= _check("t13 own entities", calls == ["sensor.charger_0", ENERGY_SENSOR], "fetched {}".format(calls))
    failed |= _check("t13 car 1", abs(car_1_kwh - 9.0) < 0.001, "kwh {}".format(car_1_kwh))
    _one_car(my_predbat, {"car_charging_energy": ENERGY_SENSOR})

    print("Test 14: the SoC never passes the battery size")
    my_predbat.expose_config("car_charging_manual_soc_kwh", 59.0)
    my_predbat.car_charging_energy = _history([(5, 5.0)])
    _changed(my_predbat, 60)
    my_predbat.car_manual_soc_track()
    failed |= _check("t14 capped", abs(_manual_kwh(my_predbat) - 60.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 15: the end-of-cycle write-back leaves an energy-tracked car alone")
    my_predbat.expose_config("car_charging_manual_soc_kwh", 20.0)
    my_predbat.car_charging_soc = [20.0]
    my_predbat.car_charging_soc_next = [35.0]
    my_predbat.update_car_manual_soc()
    failed |= _check("t15 no predicted write", abs(_manual_kwh(my_predbat) - 20.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 16: without an energy sensor the predicted SoC is still written back")
    _one_car(my_predbat, {})
    my_predbat.car_charging_soc = [20.0]
    my_predbat.car_charging_soc_next = [35.0]
    my_predbat.update_car_manual_soc()
    failed |= _check("t16 predicted write", abs(_manual_kwh(my_predbat) - 35.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    return failed


def _run_unplug(my_predbat):
    """
    The manual SoC goes back to zero when the car's plugged-in sensor has changed to unplugged since the SoC last changed.
    """
    failed = False
    dummy = my_predbat.ha_interface.dummy_items
    _one_car(my_predbat, {"car_charging_planned": PLUGGED_SENSOR})
    my_predbat.expose_config("car_charging_manual_soc_kwh", 30.0)

    print("Test 17: plugged in keeps the value")
    _changed(my_predbat, 60)
    dummy[PLUGGED_SENSOR] = {"state": "on", "last_changed": _ago(my_predbat, 5)}
    my_predbat.car_manual_soc_track()
    failed |= _check("t17 plugged", abs(_manual_kwh(my_predbat) - 30.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 18: unplugged since the value last changed resets it to zero - with nothing remembered, as after a restart")
    _changed(my_predbat, 60)
    dummy[PLUGGED_SENSOR] = {"state": "off", "last_changed": _ago(my_predbat, 5)}
    my_predbat.car_manual_soc_track()
    failed |= _check("t18 reset", abs(_manual_kwh(my_predbat)) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t18 soc", abs(my_predbat.car_charging_soc[0]) < 0.001, "soc {}".format(my_predbat.car_charging_soc))

    print("Test 19: a value entered while unplugged is kept")
    my_predbat.expose_config("car_charging_manual_soc_kwh", 25.0)
    _changed(my_predbat, 2)
    my_predbat.car_manual_soc_track()
    failed |= _check("t19 kept", abs(_manual_kwh(my_predbat) - 25.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 20: an unavailable plugged-in sensor is no evidence")
    _changed(my_predbat, 60)
    dummy[PLUGGED_SENSOR] = {"state": "unavailable", "last_changed": _ago(my_predbat, 5)}
    my_predbat.car_manual_soc_track()
    failed |= _check("t20 unavailable", abs(_manual_kwh(my_predbat) - 25.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 21: the predicted write-back skips an unplugged car, so the next cycle still sees the unplug")
    dummy[PLUGGED_SENSOR] = {"state": "off", "last_changed": _ago(my_predbat, 1)}
    my_predbat.car_charging_soc = [25.0]
    my_predbat.car_charging_soc_next = [32.0]
    my_predbat.update_car_manual_soc()
    failed |= _check("t21 no write", abs(_manual_kwh(my_predbat) - 25.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 22a: car_charging_plugged is used before car_charging_planned - a finished charge is not an unplug")
    _one_car(my_predbat, {"car_charging_planned": PLANNED_SENSOR, "car_charging_plugged": PLUGGED_SENSOR})
    my_predbat.expose_config("car_charging_manual_soc_kwh", 30.0)
    _changed(my_predbat, 60)
    dummy[PLANNED_SENSOR] = {"state": "off", "last_changed": _ago(my_predbat, 5)}
    dummy[PLUGGED_SENSOR] = {"state": "on", "last_changed": _ago(my_predbat, 120)}
    my_predbat.car_manual_soc_track()
    failed |= _check("t22a still plugged", abs(_manual_kwh(my_predbat) - 30.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    my_predbat.car_charging_soc = [30.0]
    my_predbat.car_charging_soc_next = [32.0]
    my_predbat.update_car_manual_soc()
    failed |= _check("t22a predicted write", abs(_manual_kwh(my_predbat) - 32.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 22b: car_charging_plugged turning off resets")
    _changed(my_predbat, 60)
    dummy[PLUGGED_SENSOR] = {"state": "off", "last_changed": _ago(my_predbat, 5)}
    my_predbat.car_manual_soc_track()
    failed |= _check("t22b reset", abs(_manual_kwh(my_predbat)) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 22: without car_charging_planned configured there is no reset")
    _one_car(my_predbat, {})
    my_predbat.expose_config("car_charging_manual_soc_kwh", 30.0)
    _changed(my_predbat, 60)
    my_predbat.car_manual_soc_track()
    failed |= _check("t22 no reset", abs(_manual_kwh(my_predbat) - 30.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    return failed


def _run_fetch(my_predbat):
    """
    fetch_sensor_data_cars() tracks the manual SoC for the live plan only, not for compare.py's re-runs.
    """
    failed = False
    dummy = my_predbat.ha_interface.dummy_items
    _one_car(my_predbat, {"car_charging_energy": ENERGY_SENSOR})
    my_predbat.args.pop("octopus_intelligent_slot", None)
    my_predbat.expose_config("car_charging_manual_soc_kwh", 10.0)
    _changed(my_predbat, 60)
    my_predbat.car_charging_energy = _history([(10, 2.0)])

    print("Test 23: a compare.py re-run (save=False) reads the manual SoC without moving it")
    my_predbat.fetch_sensor_data_cars(save=False)
    failed |= _check("t23 kwh", abs(_manual_kwh(my_predbat) - 10.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t23 soc", abs(my_predbat.car_charging_soc[0] - 10.0) < 0.001, "soc {}".format(my_predbat.car_charging_soc))

    print("Test 24: the live fetch adds the energy and plans from the result")
    my_predbat.fetch_sensor_data_cars(save=True)
    # fetch_sensor_data_cars() takes the charging loss from config
    expected = 10.0 + 2.0 * (1 - float(my_predbat.get_arg("car_charging_loss")))
    failed |= _check("t24 kwh", abs(_manual_kwh(my_predbat) - expected) < 0.001, "kwh {} expected {}".format(_manual_kwh(my_predbat), expected))
    failed |= _check("t24 soc", abs(my_predbat.car_charging_soc[0] - expected) < 0.001, "soc {} expected {}".format(my_predbat.car_charging_soc, expected))

    print("Test 25: a car read from its SoC sensor shows the sensor's level in the manual kWh entity")
    _one_car(my_predbat, {"car_charging_soc": SOC_SENSOR})
    dummy[SOC_SENSOR] = 50.0
    my_predbat.expose_config("car_charging_manual_soc_kwh", 5.0)
    my_predbat.fetch_sensor_data_cars(save=False)
    failed |= _check("t25 compare run publishes nothing", abs(_manual_kwh(my_predbat) - 5.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    my_predbat.fetch_sensor_data_cars(save=True)
    failed |= _check("t25 mirrored", abs(_manual_kwh(my_predbat) - 30.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t25 soc", abs(my_predbat.car_charging_soc[0] - 30.0) < 0.001, "soc {}".format(my_predbat.car_charging_soc))

    print("Test 26: with the switch forcing manual SoC the sensor is ignored and the kWh value kept")
    my_predbat.expose_config("car_charging_manual_soc", True)
    my_predbat.car_manual_soc_config()
    my_predbat.expose_config("car_charging_manual_soc_kwh", 12.0)
    my_predbat.fetch_sensor_data_cars(save=True)
    failed |= _check("t26 kept", abs(_manual_kwh(my_predbat) - 12.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t26 soc", abs(my_predbat.car_charging_soc[0] - 12.0) < 0.001, "soc {}".format(my_predbat.car_charging_soc))
    return failed


def run_car_manual_soc_tests(my_predbat):
    """
    Run the manual car SoC tests, restoring the shared fixture afterwards.
    """
    print("**** Running car_manual_soc tests ****")
    saved = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in STATE_FIELDS}
    saved_args = {arg: copy.deepcopy(my_predbat.args[arg]) for arg in SAVED_ARGS + ("octopus_intelligent_slot",) if arg in my_predbat.args}
    saved_config = {name: my_predbat.get_arg(name, None) for name in SAVED_CONFIG}
    dummy = my_predbat.ha_interface.dummy_items
    saved_dummy = {entity: copy.deepcopy(dummy.get(entity)) for entity in (ENERGY_SENSOR, PLUGGED_SENSOR, PLANNED_SENSOR, SOC_SENSOR, KWH_ENTITY)}

    failed = False
    try:
        failed |= _run_config(my_predbat)
        failed |= _run_switch(my_predbat)
        failed |= _run_energy(my_predbat)
        failed |= _run_unplug(my_predbat)
        failed |= _run_fetch(my_predbat)
    finally:
        for arg in SAVED_ARGS + ("octopus_intelligent_slot",):
            my_predbat.args.pop(arg, None)
        my_predbat.args.update(saved_args)
        for name in ("car_charging_manual_soc", "car_charging_manual_soc_1", "car_charging_manual_soc_kwh", "car_charging_manual_soc_kwh_1"):
            my_predbat.expose_config(name, saved_config[name])
        for entity, value in saved_dummy.items():
            if value is None:
                dummy.pop(entity, None)
            else:
                dummy[entity] = value
        for field, value in saved.items():
            setattr(my_predbat, field, value)

    if failed:
        print("**** car_manual_soc tests FAILED ****")
    else:
        print("**** car_manual_soc tests PASSED ****")
    return failed
