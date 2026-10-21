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

- A car with no car_charging_soc sensor is tracked manually when it has a car_charging_energy sensor and a
  plugged-in sensor. Without both it is planned from empty as before - tracking it from the plan's prediction
  would climb to the charge limit and stay there. The switch forces manual tracking for any car.
- An energy-tracked car's manual SoC rises by the energy the charger's history shows, not the plan's prediction.
- A manual SoC car is reset to zero when its plugged-in sensor goes from plugged in to unplugged.
- A value the user enters is a fresh starting point.

Each car's record is published as sensor.predbat_car_manual_soc_tracked[_N], which a restart reads back, so tracking
picks up where it left off. No entity's last_changed is used.
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
    "car_manual_soc_state",
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


def _history(points):
    """
    A cumulative kWh series indexed by minutes ago, as minute_data() builds it, from (minutes ago, kWh charged
    in the minute before) pairs - each pair adds its energy to the total from that minute onwards.
    """
    history = {}
    for minute in range(24 * 60):
        history[minute] = sum(kwh for ago, kwh in points if ago >= minute)
    return history


def _since(my_predbat, minutes_ago, car_n=0):
    """
    Move car_n's record back so it counts from minutes_ago, as if that many minutes had passed since the last cycle.
    """
    my_predbat.car_manual_soc_state[car_n]["since"] = (my_predbat.now_utc - timedelta(minutes=minutes_ago)).isoformat()


def _plug(my_predbat, state, entity=PLUGGED_SENSOR):
    """
    Set the plugged-in sensor's state.
    """
    my_predbat.ha_interface.dummy_items[entity] = {"state": state}


def _restart(my_predbat, keep_sensor=True):
    """
    Forget everything held in memory, as a restart does - keeping car 0's tracking sensor, or losing it too.
    """
    my_predbat.car_manual_soc_state = {}
    if not keep_sensor:
        my_predbat.ha_interface.dummy_items.pop(my_predbat.car_manual_soc_entity(0), None)


def _one_car(my_predbat, args, kwh=None):
    """
    A single car, with apps.yaml holding only the given car args and no tracking record or sensor.
    """
    for arg in SAVED_ARGS:
        my_predbat.args.pop(arg, None)
    my_predbat.args.update(args)
    my_predbat.num_cars = 1
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_energy_scale = 1.0
    my_predbat.car_charging_battery_size = [60.0]
    my_predbat.car_charging_limit = [60.0]
    my_predbat.car_charging_soc = [0.0]
    # Car 0's one energy sensor reads the history already loaded for the load forecast - none charged so far
    my_predbat.car_charging_energy = _history([])
    _restart(my_predbat, keep_sensor=False)
    my_predbat.expose_config("car_charging_manual_soc", False)
    my_predbat.car_manual_soc_config()
    if kwh is not None:
        my_predbat.expose_config("car_charging_manual_soc_kwh", kwh)


def _run_config(my_predbat):
    """
    car_manual_soc_config() turns manual SoC on automatically only for a car it can track from what really happens.
    """
    failed = False
    my_predbat.expose_config("car_charging_manual_soc", False)
    my_predbat.expose_config("car_charging_manual_soc_1", False)

    def manual(args, num_cars=1):
        """
        car_charging_manual_soc and car_charging_manual_soc_energy for the given car args.
        """
        for arg in SAVED_ARGS:
            my_predbat.args.pop(arg, None)
        my_predbat.args.update(args)
        my_predbat.num_cars = num_cars
        my_predbat.car_manual_soc_config()
        return my_predbat.car_charging_manual_soc[:num_cars], my_predbat.car_charging_manual_soc_energy[:num_cars]

    print("Test 1: no sensors at all, as an Octopus Intelligent car usually has - not manual, planned from empty as before")
    result = manual({})
    failed |= _check("t1 no sensors", result == ([False], [False]), "got {}".format(result))

    print("Test 2: an energy sensor alone, or a plugged-in sensor alone, is not enough")
    result = manual({"car_charging_energy": ENERGY_SENSOR})
    failed |= _check("t2 energy only", result == ([False], [False]), "got {}".format(result))
    result = manual({"car_charging_planned": PLANNED_SENSOR})
    failed |= _check("t2 plug only", result == ([False], [False]), "got {}".format(result))

    print("Test 3: no SoC sensor, with energy and plugged-in sensors - manual and energy-tracked")
    result = manual({"car_charging_energy": ENERGY_SENSOR, "car_charging_planned": PLANNED_SENSOR})
    failed |= _check("t3 planned", result == ([True], [True]), "got {}".format(result))
    result = manual({"car_charging_energy": ENERGY_SENSOR, "car_charging_plugged": PLUGGED_SENSOR})
    failed |= _check("t3 plugged", result == ([True], [True]), "got {}".format(result))

    print("Test 4: a fixed plugged-in value is not a sensor - it can never read unplugged")
    result = manual({"car_charging_energy": ENERGY_SENSOR, "car_charging_planned": "on"})
    failed |= _check("t4 literal", result == ([False], [False]), "got {}".format(result))

    print("Test 5: a car with a SoC sensor is left on it unless the switch is on")
    result = manual({"car_charging_soc": SOC_SENSOR, "car_charging_energy": ENERGY_SENSOR, "car_charging_planned": PLANNED_SENSOR})
    failed |= _check("t5 sensor", result == ([False], [False]), "got {}".format(result))
    my_predbat.expose_config("car_charging_manual_soc", True)
    result = manual({"car_charging_soc": SOC_SENSOR, "car_charging_energy": ENERGY_SENSOR, "car_charging_planned": PLANNED_SENSOR})
    failed |= _check("t5 switch", result == ([True], [True]), "got {}".format(result))

    print("Test 6: the switch still makes a car with no sensors manual, tracked from the plan")
    result = manual({})
    failed |= _check("t6 switch no sensors", result == ([True], [False]), "got {}".format(result))
    my_predbat.expose_config("car_charging_manual_soc", False)

    print("Test 7: per car - a list with no SoC entry for car 1, and an unresolved regular expression")
    result = manual({"car_charging_soc": [SOC_SENSOR], "car_charging_energy": ["sensor.charger_0", ENERGY_SENSOR], "car_charging_planned": [PLANNED_SENSOR, PLUGGED_SENSOR]}, num_cars=2)
    failed |= _check("t7 short list", result == ([False, True], [False, True]), "got {}".format(result))
    result = manual({"car_charging_soc": "re:sensor.no_such_car", "car_charging_energy": ENERGY_SENSOR, "car_charging_planned": PLANNED_SENSOR})
    failed |= _check("t7 regex", result == ([True], [True]), "got {}".format(result))
    return failed


def _run_energy(my_predbat):
    """
    The manual SoC rises by the energy the charger's history shows since the record's time.
    """
    failed = False
    _one_car(my_predbat, {"car_charging_energy": ENERGY_SENSOR, "car_charging_planned": PLANNED_SENSOR}, kwh=10.0)
    _plug(my_predbat, "on", PLANNED_SENSOR)
    # 1 kWh 3 hours ago, then 2 kWh over the last half hour
    my_predbat.car_charging_energy = _history([(180, 1.0), (20, 1.0), (5, 1.0)])

    print("Test 8: a car seen for the first time counts from now - nothing in the history before is added")
    my_predbat.car_manual_soc_track()
    failed |= _check("t8 nothing", abs(_manual_kwh(my_predbat) - 10.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t8 record", my_predbat.car_manual_soc_state.get(0, {}).get("soc") == 10.0, "record {}".format(my_predbat.car_manual_soc_state))

    print("Test 9: the energy since the record's time is added, less charging losses")
    _since(my_predbat, 60)
    my_predbat.car_charging_loss = 0.9
    my_predbat.car_manual_soc_track()
    failed |= _check("t9 added", abs(_manual_kwh(my_predbat) - 11.8) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t9 soc", abs(my_predbat.car_charging_soc[0] - 11.8) < 0.001, "soc {}".format(my_predbat.car_charging_soc))
    my_predbat.car_charging_loss = 1.0

    print("Test 10: the next cycle adds nothing twice")
    my_predbat.car_manual_soc_track()
    failed |= _check("t10 not twice", abs(_manual_kwh(my_predbat) - 11.8) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 11: a value the user entered is kept, with nothing added on top of it")
    my_predbat.expose_config("car_charging_manual_soc_kwh", 20.0)
    _since(my_predbat, 60)
    my_predbat.car_manual_soc_track()
    failed |= _check("t11 kept", abs(_manual_kwh(my_predbat) - 20.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    print("Test 11b: ... and counted on from when it was entered")
    my_predbat.car_charging_energy = _history([(180, 1.0), (20, 1.0), (5, 1.0), (1, 1.5)])
    _since(my_predbat, 3)
    my_predbat.car_manual_soc_track()
    failed |= _check("t11b counted on", abs(_manual_kwh(my_predbat) - 21.5) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 12: a time older than the history counts the whole history")
    my_predbat.car_charging_energy = _history([(180, 1.0), (20, 1.0), (5, 1.0)])
    _one_car(my_predbat, {"car_charging_energy": ENERGY_SENSOR, "car_charging_planned": PLANNED_SENSOR}, kwh=20.0)
    my_predbat.car_charging_energy = _history([(180, 1.0), (20, 1.0), (5, 1.0)])
    my_predbat.car_manual_soc_track()
    _since(my_predbat, 3 * 24 * 60)
    my_predbat.car_manual_soc_track()
    failed |= _check("t12 whole history", abs(_manual_kwh(my_predbat) - 23.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 13: with nothing charged the record keeps counting from no more than an hour ago")
    my_predbat.car_charging_energy = _history([])
    _since(my_predbat, 3 * 24 * 60)
    my_predbat.car_manual_soc_track()
    since = my_predbat.car_manual_soc_state[0]["since"]
    expected = (my_predbat.now_utc - timedelta(minutes=60)).isoformat()
    failed |= _check("t13 since", since == expected, "since {} expected {}".format(since, expected))
    failed |= _check("t13 kwh", abs(_manual_kwh(my_predbat) - 23.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 14: no history adds nothing")
    my_predbat.car_charging_energy = {}
    saved_fetch = my_predbat.minute_data_import_export
    my_predbat.minute_data_import_export = lambda *args, **kwargs: {}
    try:
        _since(my_predbat, 60)
        my_predbat.car_manual_soc_track()
    finally:
        my_predbat.minute_data_import_export = saved_fetch
    failed |= _check("t14 nothing", abs(_manual_kwh(my_predbat) - 23.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 15: the SoC never passes the battery size")
    my_predbat.expose_config("car_charging_manual_soc_kwh", 59.0)
    my_predbat.car_manual_soc_track()
    my_predbat.car_charging_energy = _history([(5, 5.0)])
    _since(my_predbat, 60)
    my_predbat.car_manual_soc_track()
    failed |= _check("t15 capped", abs(_manual_kwh(my_predbat) - 60.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 16: a second car fetches its own charger's history, not the summed one")
    calls = []

    def fake_fetch(days, now_utc, key, scale=1.0, required_unit=None, **kwargs):
        """
        Record the entity asked for and return 4 kWh charged in the last 10 minutes.
        """
        calls.append(key)
        return _history([(10, 4.0)])

    # The car 1 kWh entity is enabled by the num_cars setting
    saved_num_cars = my_predbat.args.get("num_cars", None)
    my_predbat.args["num_cars"] = 2
    my_predbat.num_cars = 2
    my_predbat.args["car_charging_energy"] = ["sensor.charger_0", ENERGY_SENSOR]
    my_predbat.args["car_charging_planned"] = [PLANNED_SENSOR, PLANNED_SENSOR]
    my_predbat.car_charging_battery_size = [60.0, 60.0]
    my_predbat.car_charging_soc = [0.0, 0.0]
    my_predbat.car_manual_soc_config()
    my_predbat.expose_config("car_charging_manual_soc_kwh", 20.0)
    my_predbat.expose_config("car_charging_manual_soc_kwh_1", 5.0)
    my_predbat.minute_data_import_export = fake_fetch
    try:
        my_predbat.car_manual_soc_track()
        _since(my_predbat, 60, 0)
        _since(my_predbat, 60, 1)
        calls.clear()
        my_predbat.car_manual_soc_track()
        car_1_kwh = _manual_kwh(my_predbat, "_1")
    finally:
        my_predbat.minute_data_import_export = saved_fetch
        if saved_num_cars is None:
            my_predbat.args.pop("num_cars", None)
        else:
            my_predbat.args["num_cars"] = saved_num_cars
    failed |= _check("t16 own entities", calls == ["sensor.charger_0", ENERGY_SENSOR], "fetched {}".format(calls))
    failed |= _check("t16 car 1", abs(car_1_kwh - 9.0) < 0.001, "kwh {}".format(car_1_kwh))

    print("Test 17: the end-of-cycle write-back leaves an energy-tracked car alone")
    _one_car(my_predbat, {"car_charging_energy": ENERGY_SENSOR, "car_charging_planned": PLANNED_SENSOR}, kwh=20.0)
    my_predbat.car_charging_soc = [20.0]
    my_predbat.car_charging_soc_next = [35.0]
    my_predbat.update_car_manual_soc()
    failed |= _check("t17 no predicted write", abs(_manual_kwh(my_predbat) - 20.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    return failed


def _run_write_back(my_predbat):
    """
    A switched-on car with no energy sensor is still moved on by the plan, and that write is not taken for a user edit.
    """
    failed = False
    print("Test 18: without an energy sensor the predicted SoC is written back")
    _one_car(my_predbat, {"car_charging_planned": PLANNED_SENSOR}, kwh=20.0)
    my_predbat.expose_config("car_charging_manual_soc", True)
    my_predbat.car_manual_soc_config()
    _plug(my_predbat, "on", PLANNED_SENSOR)
    my_predbat.car_manual_soc_track()
    my_predbat.car_charging_soc = [20.0]
    my_predbat.car_charging_soc_next = [35.0]
    my_predbat.update_car_manual_soc()
    failed |= _check("t18 predicted write", abs(_manual_kwh(my_predbat) - 35.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t18 recorded", my_predbat.car_manual_soc_state[0]["soc"] == 35.0, "record {}".format(my_predbat.car_manual_soc_state))

    print("Test 19: ... so an unplug on the next cycle still resets it")
    _plug(my_predbat, "off", PLANNED_SENSOR)
    my_predbat.car_manual_soc_track()
    failed |= _check("t19 reset", abs(_manual_kwh(my_predbat)) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 20: the write-back skips an unplugged car")
    my_predbat.car_charging_soc = [0.0]
    my_predbat.car_charging_soc_next = [7.0]
    my_predbat.update_car_manual_soc()
    failed |= _check("t20 no write", abs(_manual_kwh(my_predbat)) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    my_predbat.expose_config("car_charging_manual_soc", False)
    return failed


def _run_unplug(my_predbat):
    """
    The manual SoC goes back to zero when the car's plugged-in sensor goes from plugged in to unplugged.
    """
    failed = False
    args = {"car_charging_energy": ENERGY_SENSOR, "car_charging_planned": PLUGGED_SENSOR}

    print("Test 21: plugged in keeps the value")
    _one_car(my_predbat, args, kwh=30.0)
    _plug(my_predbat, "on")
    my_predbat.car_manual_soc_track()
    my_predbat.car_manual_soc_track()
    failed |= _check("t21 plugged", abs(_manual_kwh(my_predbat) - 30.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 22: an unavailable sensor is no reading - nothing is reset and the last reading is kept")
    _plug(my_predbat, "unavailable")
    my_predbat.car_manual_soc_track()
    failed |= _check("t22 unavailable", abs(_manual_kwh(my_predbat) - 30.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t22 plugged kept", my_predbat.car_manual_soc_state[0]["plugged"] is True, "record {}".format(my_predbat.car_manual_soc_state))

    print("Test 23: plugged in to unplugged resets to zero")
    _plug(my_predbat, "off")
    my_predbat.car_manual_soc_track()
    failed |= _check("t23 reset", abs(_manual_kwh(my_predbat)) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t23 soc", abs(my_predbat.car_charging_soc[0]) < 0.001, "soc {}".format(my_predbat.car_charging_soc))

    print("Test 24: a value entered while unplugged is kept")
    my_predbat.expose_config("car_charging_manual_soc_kwh", 25.0)
    my_predbat.car_manual_soc_track()
    my_predbat.car_manual_soc_track()
    failed |= _check("t24 kept", abs(_manual_kwh(my_predbat) - 25.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 25: a car first seen unplugged is not reset")
    _one_car(my_predbat, args, kwh=30.0)
    _plug(my_predbat, "off")
    my_predbat.car_manual_soc_track()
    failed |= _check("t25 first seen", abs(_manual_kwh(my_predbat) - 30.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 26: car_charging_plugged is used before car_charging_planned - a finished charge is not an unplug")
    _one_car(my_predbat, {"car_charging_energy": ENERGY_SENSOR, "car_charging_planned": PLANNED_SENSOR, "car_charging_plugged": PLUGGED_SENSOR}, kwh=30.0)
    _plug(my_predbat, "on", PLANNED_SENSOR)
    _plug(my_predbat, "on")
    my_predbat.car_manual_soc_track()
    _plug(my_predbat, "off", PLANNED_SENSOR)
    my_predbat.car_manual_soc_track()
    failed |= _check("t26 still plugged", abs(_manual_kwh(my_predbat) - 30.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    _plug(my_predbat, "off")
    my_predbat.car_manual_soc_track()
    failed |= _check("t26 reset", abs(_manual_kwh(my_predbat)) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    return failed


def _run_restart(my_predbat):
    """
    The record is published as a sensor, which a restart reads back, so a restart neither loses nor double counts.
    """
    failed = False
    args = {"car_charging_energy": ENERGY_SENSOR, "car_charging_planned": PLUGGED_SENSOR}
    sensor = my_predbat.car_manual_soc_entity(0)
    dummy = my_predbat.ha_interface.dummy_items

    print("Test 27: the record is published as the car's tracking sensor")
    _one_car(my_predbat, args, kwh=10.0)
    _plug(my_predbat, "on")
    my_predbat.car_manual_soc_track()
    my_predbat.expose_config("car_charging_manual_soc_kwh", 10.5)
    my_predbat.car_manual_soc_track()
    published = dummy.get(sensor, {})
    record = my_predbat.car_manual_soc_state[0]
    failed |= _check("t27 state", published.get("state") == 10.5, "sensor {}".format(published))
    failed |= _check("t27 since", published.get("since") == record["since"], "sensor {} record {}".format(published, record))
    failed |= _check("t27 plugged", published.get("plugged") is True, "sensor {}".format(published))

    print("Test 28: energy charged while Predbat was down is counted after a restart")
    # Two hours pass - the sensor holds the time counting got to, as it would after a real outage
    dummy[sensor]["since"] = (my_predbat.now_utc - timedelta(minutes=120)).isoformat()
    _restart(my_predbat)
    my_predbat.car_charging_energy = _history([(90, 3.0), (30, 3.0)])
    my_predbat.car_manual_soc_track()
    failed |= _check("t28 counted", abs(_manual_kwh(my_predbat) - 16.5) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 29: ... and not again after a second restart")
    _restart(my_predbat)
    my_predbat.car_manual_soc_track()
    failed |= _check("t29 not twice", abs(_manual_kwh(my_predbat) - 16.5) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 30: a value entered while Predbat was down is taken as an edit")
    _restart(my_predbat)
    my_predbat.expose_config("car_charging_manual_soc_kwh", 30.0)
    dummy[sensor]["since"] = (my_predbat.now_utc - timedelta(minutes=120)).isoformat()
    my_predbat.car_manual_soc_track()
    failed |= _check("t30 edit kept", abs(_manual_kwh(my_predbat) - 30.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 31: an unplug while Predbat was down resets it after the restart")
    _restart(my_predbat)
    _plug(my_predbat, "off")
    my_predbat.car_manual_soc_track()
    failed |= _check("t31 reset", abs(_manual_kwh(my_predbat)) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 32: with no sensor to read back a restart counts from now")
    _one_car(my_predbat, args, kwh=10.0)
    _plug(my_predbat, "on")
    my_predbat.car_manual_soc_track()
    _since(my_predbat, 120)
    _restart(my_predbat, keep_sensor=False)
    my_predbat.car_charging_energy = _history([(90, 3.0)])
    my_predbat.car_manual_soc_track()
    failed |= _check("t32 from now", abs(_manual_kwh(my_predbat) - 10.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 33: a sensor that cannot be read is no record")
    _one_car(my_predbat, args, kwh=10.0)
    dummy[sensor] = {"state": "unavailable", "since": (my_predbat.now_utc - timedelta(minutes=120)).isoformat(), "plugged": True}
    my_predbat.car_charging_energy = _history([(90, 3.0)])
    my_predbat.car_manual_soc_track()
    failed |= _check("t33 from now", abs(_manual_kwh(my_predbat) - 10.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))

    print("Test 34: a cycle where nothing changes publishes nothing")
    _one_car(my_predbat, args, kwh=10.0)
    my_predbat.car_charging_energy = _history([])
    published = []
    my_predbat.car_manual_soc_publish = lambda car_n: published.append(car_n)
    try:
        my_predbat.car_manual_soc_track()
        first = len(published)
        _since(my_predbat, 120)
        my_predbat.car_manual_soc_track()
    finally:
        del my_predbat.car_manual_soc_publish
    failed |= _check("t34 published once", first == 1 and len(published) == 1, "published {}".format(published))
    return failed


def _run_fetch(my_predbat):
    """
    fetch_sensor_data_cars() tracks the manual SoC for the live plan only, not for compare.py's re-runs.
    """
    failed = False
    dummy = my_predbat.ha_interface.dummy_items
    _one_car(my_predbat, {"car_charging_energy": ENERGY_SENSOR, "car_charging_planned": PLUGGED_SENSOR}, kwh=10.0)
    my_predbat.args.pop("octopus_intelligent_slot", None)
    _plug(my_predbat, "on")
    my_predbat.car_charging_energy = _history([(10, 2.0)])
    my_predbat.car_manual_soc_track()
    _since(my_predbat, 60)

    print("Test 35: a compare.py re-run (save=False) reads the manual SoC without moving it")
    my_predbat.fetch_sensor_data_cars(save=False)
    failed |= _check("t35 kwh", abs(_manual_kwh(my_predbat) - 10.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t35 soc", abs(my_predbat.car_charging_soc[0] - 10.0) < 0.001, "soc {}".format(my_predbat.car_charging_soc))

    print("Test 36: the live fetch adds the energy and plans from the result")
    my_predbat.fetch_sensor_data_cars(save=True)
    # fetch_sensor_data_cars() takes the charging loss from config
    expected = 10.0 + 2.0 * (1 - float(my_predbat.get_arg("car_charging_loss")))
    failed |= _check("t36 kwh", abs(_manual_kwh(my_predbat) - expected) < 0.001, "kwh {} expected {}".format(_manual_kwh(my_predbat), expected))
    failed |= _check("t36 soc", abs(my_predbat.car_charging_soc[0] - expected) < 0.001, "soc {} expected {}".format(my_predbat.car_charging_soc, expected))

    print("Test 37: a car with no sensors is planned from empty and never written back - no climb to full")
    _one_car(my_predbat, {}, kwh=0.0)
    my_predbat.fetch_sensor_data_cars(save=True)
    failed |= _check("t37 soc", abs(my_predbat.car_charging_soc[0]) < 0.001, "soc {}".format(my_predbat.car_charging_soc))
    my_predbat.car_charging_soc_next = [7.0]
    my_predbat.update_car_manual_soc()
    my_predbat.fetch_sensor_data_cars(save=True)
    failed |= _check("t37 no climb", abs(my_predbat.car_charging_soc[0]) < 0.001, "soc {}".format(my_predbat.car_charging_soc))

    print("Test 38: a car read from its SoC sensor shows the sensor's level in the manual kWh entity")
    _one_car(my_predbat, {"car_charging_soc": SOC_SENSOR}, kwh=5.0)
    dummy[SOC_SENSOR] = 50.0
    my_predbat.fetch_sensor_data_cars(save=False)
    failed |= _check("t38 compare run publishes nothing", abs(_manual_kwh(my_predbat) - 5.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    my_predbat.fetch_sensor_data_cars(save=True)
    failed |= _check("t38 mirrored", abs(_manual_kwh(my_predbat) - 30.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t38 soc", abs(my_predbat.car_charging_soc[0] - 30.0) < 0.001, "soc {}".format(my_predbat.car_charging_soc))

    print("Test 39: with the switch forcing manual SoC the sensor is ignored and the kWh value kept")
    my_predbat.expose_config("car_charging_manual_soc", True)
    my_predbat.car_manual_soc_config()
    my_predbat.expose_config("car_charging_manual_soc_kwh", 12.0)
    my_predbat.fetch_sensor_data_cars(save=True)
    failed |= _check("t39 kept", abs(_manual_kwh(my_predbat) - 12.0) < 0.001, "kwh {}".format(_manual_kwh(my_predbat)))
    failed |= _check("t39 soc", abs(my_predbat.car_charging_soc[0] - 12.0) < 0.001, "soc {}".format(my_predbat.car_charging_soc))
    my_predbat.expose_config("car_charging_manual_soc", False)
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
    tracked = tuple(my_predbat.car_manual_soc_entity(car_n) for car_n in range(2))
    saved_dummy = {entity: copy.deepcopy(dummy.get(entity)) for entity in (ENERGY_SENSOR, PLUGGED_SENSOR, PLANNED_SENSOR, SOC_SENSOR) + tracked}

    failed = False
    try:
        failed |= _run_config(my_predbat)
        failed |= _run_energy(my_predbat)
        failed |= _run_write_back(my_predbat)
        failed |= _run_unplug(my_predbat)
        failed |= _run_restart(my_predbat)
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
