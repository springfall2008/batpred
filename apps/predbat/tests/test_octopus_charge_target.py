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
The Octopus Intelligent charge target is a percentage to add when the charger is linked, not a SoC (#3034).

With the car linked Octopus's target is the SoC to charge the car to. With the charger linked Octopus
cannot see the car, so the same number is how much to add. Read as a SoC, a car plugged in at 39% with
"add 40%" was taken as already charged as soon as the charge Octopus does at plug-in took it past 40%,
and its Intelligent slots were then ignored.
"""
import copy
from datetime import timedelta

IOG_SENSOR = "binary_sensor.octopus_charge_target_test"
BATTERY_KWH = 62.0

STATE_FIELDS = (
    "num_cars",
    "car_charging_now",
    "car_charging_planned",
    "car_charging_soc",
    "car_charging_soc_next",
    "car_charging_limit",
    "car_charging_limit_model",
    "car_charging_rate",
    "car_charging_loss",
    "car_charging_plan_time",
    "car_charging_slots",
    "car_charging_manual_soc",
    "car_charging_battery_size",
    "octopus_slots",
    "octopus_intelligent_charging",
    "octopus_intelligent_ignore_unplugged",
    "octopus_charge_target_base",
    "dispatch_timeline_pending",
    "rate_import",
)
SAVED_ARGS = ("octopus_intelligent_slot", "octopus_charge_limit", "octopus_ready_time", "car_charging_soc", "car_charging_limit")


def _check(name, condition, detail=""):
    """
    Print an error for a failed condition and return whether it failed.
    """
    if not condition:
        print("ERROR: {} {}".format(name, detail))
        return True
    return False


def _sensor(my_predbat, attributes, planned=None):
    """
    Point the Octopus Intelligent slot at a dispatch sensor carrying these attributes and planned dispatches.
    """
    my_predbat.args["octopus_intelligent_slot"] = IOG_SENSOR
    my_predbat.ha_interface.set_state(IOG_SENSOR, "off", attributes={"planned_dispatches": planned or [], "completed_dispatches": [], **attributes})


def _fetch(my_predbat, soc_percent, target_percent, plugged=True, save=True, car_limit_percent=100, charging_now=False, ignore_unplugged=False):
    """
    Run the car fetch for one car at soc_percent with an Octopus target of target_percent, returning its charge limit.
    """
    my_predbat.num_cars = 1
    my_predbat.car_charging_now = [charging_now]
    my_predbat.car_charging_planned = [plugged]
    my_predbat.car_charging_manual_soc = [False]
    my_predbat.car_charging_battery_size = [BATTERY_KWH]
    my_predbat.car_charging_limit = [BATTERY_KWH]
    my_predbat.car_charging_rate = [7.4]
    my_predbat.car_charging_plan_time = ["07:00:00"]
    my_predbat.car_charging_slots = [[]]
    my_predbat.octopus_slots = [[]]
    my_predbat.octopus_intelligent_charging = True
    my_predbat.octopus_intelligent_ignore_unplugged = ignore_unplugged
    my_predbat.dispatch_timeline_pending = []
    # No rates, so the fetch leaves the shared fixture's rate statistics alone
    my_predbat.rate_import = {}
    my_predbat.args["car_charging_soc"] = soc_percent
    my_predbat.args["car_charging_limit"] = car_limit_percent
    my_predbat.args["octopus_charge_limit"] = target_percent
    my_predbat.args.pop("octopus_ready_time", None)
    my_predbat.fetch_sensor_data_cars(save=save)
    return my_predbat.car_charging_limit[0]


def _run_detection(my_predbat):
    """
    Which attribute says the target is a percentage to add, and which way round each one reads.
    """
    failed = False
    print("Test 1: telling a linked charger from a linked car")
    cases = [
        ("Predbat Octopus component, charger", {"is_charger": True, "vehicle_battery_size_in_kwh": None, "charge_point_power_in_kw": 7.0}, True),
        ("Predbat Octopus component, car", {"is_charger": False, "vehicle_battery_size_in_kwh": 75.0, "charge_point_power_in_kw": None}, False),
        ("is_charger wins over the sizes", {"is_charger": False, "vehicle_battery_size_in_kwh": None, "charge_point_power_in_kw": 7.0}, False),
        ("HA integration device_type, charger", {"device_type": "ELECTRIC_VEHICLE_CHARGERS", "vehicle_battery_size_in_kwh": None, "charge_point_power_in_kw": 7.0}, True),
        ("HA integration device_type, car", {"device_type": "ELECTRIC_VEHICLES", "vehicle_battery_size_in_kwh": 75.0, "charge_point_power_in_kw": None}, False),
        ("older HA integration, charger", {"vehicle_battery_size_in_kwh": None, "charge_point_power_in_kw": 7.0}, True),
        ("older HA integration, charger listed without a power", {"vehicle_battery_size_in_kwh": None, "charge_point_power_in_kw": 0}, True),
        ("older HA integration, car", {"vehicle_battery_size_in_kwh": 75.0, "charge_point_power_in_kw": None}, False),
        ("both sizes given", {"vehicle_battery_size_in_kwh": 75.0, "charge_point_power_in_kw": 7.0}, None),
        ("older HA integration, charger or car Octopus does not list", {"vehicle_battery_size_in_kwh": None, "charge_point_power_in_kw": None}, None),
        ("no attributes at all", {}, None),
    ]
    for name, attributes, expected in cases:
        my_predbat.ha_interface.set_state(IOG_SENSOR, "off", attributes=attributes)
        result = my_predbat.octopus_charge_target_is_added(IOG_SENSOR)
        failed |= _check("t1 " + name, result is expected, "expected {} got {}".format(expected, result))
    return failed


def _run_target_entity_detection(my_predbat):
    """
    device_type on the car's charge target entity (BottlecapDave #1917) decides, ahead of the sizes; a target set as a
    plain number has no entity to read, so the dispatch sensor alone decides. is_charger as a string is read too.
    """
    failed = False
    print("Test 1b: device_type on the charge target entity, and is_charger as a string")
    target = "number.octopus_energy_test_intelligent_charge_target"
    saved = my_predbat.args.get("octopus_charge_limit")
    try:
        my_predbat.args["octopus_charge_limit"] = target
        sizes_say_car = {"vehicle_battery_size_in_kwh": 75.0, "charge_point_power_in_kw": None}
        my_predbat.ha_interface.set_state(IOG_SENSOR, "off", attributes=sizes_say_car)
        for device_type, expected in (("ELECTRIC_VEHICLE_CHARGERS", True), ("ELECTRIC_VEHICLES", False)):
            my_predbat.ha_interface.set_state(target, "80", attributes={"device_type": device_type})
            result = my_predbat.octopus_charge_target_is_added(IOG_SENSOR, 0)
            failed |= _check("t1b target entity says {}".format(device_type), result is expected, "expected {} got {}".format(expected, result))
        my_predbat.ha_interface.set_state(target, "80", attributes={})
        result = my_predbat.octopus_charge_target_is_added(IOG_SENSOR, 0)
        failed |= _check("t1b target entity without device_type falls back to the sizes", result is False, "got {}".format(result))
        my_predbat.args["octopus_charge_limit"] = 80
        my_predbat.ha_interface.set_state(IOG_SENSOR, "off", attributes={"vehicle_battery_size_in_kwh": None, "charge_point_power_in_kw": 7.0})
        result = my_predbat.octopus_charge_target_is_added(IOG_SENSOR, 0)
        failed |= _check("t1b a plain-number target is decided by the dispatch sensor", result is True, "got {}".format(result))
        for text, expected in (("true", True), ("False", False), ("unknown", None)):
            my_predbat.ha_interface.set_state(IOG_SENSOR, "off", attributes={"is_charger": text})
            result = my_predbat.octopus_charge_target_is_added(IOG_SENSOR, 0)
            failed |= _check("t1b is_charger '{}'".format(text), result is expected, "expected {} got {}".format(expected, result))
    finally:
        if saved is None:
            my_predbat.args.pop("octopus_charge_limit", None)
        else:
            my_predbat.args["octopus_charge_limit"] = saved
    return failed


def _run_limit(my_predbat):
    """
    The charge limit a charger-linked target gives, and that a car-linked one is unchanged.
    """
    failed = False
    charger = {"vehicle_battery_size_in_kwh": None, "charge_point_power_in_kw": 7.0}
    car = {"vehicle_battery_size_in_kwh": BATTERY_KWH, "charge_point_power_in_kw": None}

    # The #3034 log: 39% of 62kWh, "add 40%", and the plug-in charge then takes the car to 43%
    print("Test 2: a charger-linked target adds to the SoC at plug-in")
    my_predbat.octopus_charge_target_base = {}
    _sensor(my_predbat, charger)
    _fetch(my_predbat, 39, 40, plugged=False)
    limit = _fetch(my_predbat, 39, 40)
    failed |= _check("t2 limit", abs(limit - (24.18 + 24.8)) < 0.01, "limit {}".format(limit))

    print("Test 3: the limit holds while the car charges, so the car is not already charged")
    limit = _fetch(my_predbat, 43, 40)
    failed |= _check("t3 limit held", abs(limit - (24.18 + 24.8)) < 0.01, "limit {}".format(limit))
    failed |= _check("t3 not full", my_predbat.car_charging_soc[0] < limit, "soc {} limit {}".format(my_predbat.car_charging_soc[0], limit))

    print("Test 4: unplugging starts the next charge from the SoC then")
    _fetch(my_predbat, 30, 40, plugged=False)
    limit = _fetch(my_predbat, 30, 40)
    failed |= _check("t4 limit", abs(limit - (18.6 + 24.8)) < 0.01, "limit {}".format(limit))

    print("Test 5: the car's own charge limit still caps it")
    limit = _fetch(my_predbat, 30, 40, car_limit_percent=60)
    failed |= _check("t5 capped", abs(limit - 37.2) < 0.01, "limit {}".format(limit))

    print("Test 6: compare.py's re-runs take the live plug-in SoC and do not record one")
    my_predbat.octopus_charge_target_base = {}
    _fetch(my_predbat, 20, 40, plugged=False)
    limit = _fetch(my_predbat, 10, 40, plugged=False, save=False)
    failed |= _check("t6 base kept", my_predbat.octopus_charge_target_base == {0: 12.4}, "base {}".format(my_predbat.octopus_charge_target_base))
    failed |= _check("t6 limit from live", abs(limit - (12.4 + 24.8)) < 0.01, "limit {}".format(limit))

    print("Test 7: a SoC sensor reading 0 for a cycle does not lose the plug-in SoC")
    _fetch(my_predbat, 20, 40)
    _fetch(my_predbat, 0, 40)
    limit = _fetch(my_predbat, 25, 40)
    failed |= _check("t7 limit", abs(limit - (12.4 + 24.8)) < 0.01, "limit {}".format(limit))

    print("Test 8: a car charging now is plugged in")
    my_predbat.octopus_charge_target_base = {}
    _fetch(my_predbat, 20, 40, plugged=False)
    limit = _fetch(my_predbat, 25, 40, plugged=False, charging_now=True)
    failed |= _check("t8 limit", abs(limit - (12.4 + 24.8)) < 0.01, "limit {}".format(limit))

    # Planned dispatches only count while Octopus keeps none for an unplugged car
    start = (my_predbat.now_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S%z")
    end = (my_predbat.now_utc + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")
    planned = [{"start": start, "end": end, "charge_in_kwh": 7.0, "source": "smart-charge", "location": "AT_HOME"}]
    print("Test 9: planned dispatches say the car is plugged in")
    my_predbat.octopus_charge_target_base = {}
    _sensor(my_predbat, charger)
    _fetch(my_predbat, 20, 40, plugged=False)
    _sensor(my_predbat, charger, planned=planned)
    limit = _fetch(my_predbat, 25, 40, plugged=False)
    failed |= _check("t9 limit", abs(limit - (12.4 + 24.8)) < 0.01, "limit {}".format(limit))

    print("Test 10: but not with octopus_intelligent_ignore_unplugged, where they outlast the plug")
    limit = _fetch(my_predbat, 50, 40, plugged=False, ignore_unplugged=True)
    failed |= _check("t10 limit", abs(limit - (31.0 + 24.8)) < 0.01, "limit {}".format(limit))

    print("Test 11: a car-linked target is still a SoC to reach")
    my_predbat.octopus_charge_target_base = {}
    _sensor(my_predbat, car)
    limit = _fetch(my_predbat, 39, 40)
    failed |= _check("t11 limit", abs(limit - 24.8) < 0.01, "limit {}".format(limit))

    print("Test 12: a sensor that does not say is read as a SoC to reach, as before")
    _sensor(my_predbat, {"vehicle_battery_size_in_kwh": BATTERY_KWH, "charge_point_power_in_kw": 7.0})
    limit = _fetch(my_predbat, 39, 40)
    failed |= _check("t12 limit", abs(limit - 24.8) < 0.01, "limit {}".format(limit))
    return failed


def test_octopus_charge_target(my_predbat):
    """
    The Octopus Intelligent charge target is a percentage to add when the charger is linked (#3034).
    """
    print("*** Running test: octopus_charge_target")
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in STATE_FIELDS}
    saved_args = {key: copy.deepcopy(my_predbat.args[key]) for key in SAVED_ARGS if key in my_predbat.args}
    try:
        failed = _run_detection(my_predbat)
        failed |= _run_target_entity_detection(my_predbat)
        failed |= _run_limit(my_predbat)
    finally:
        for field, value in saved_state.items():
            setattr(my_predbat, field, value)
        for key in SAVED_ARGS:
            if key in saved_args:
                my_predbat.args[key] = saved_args[key]
            else:
                my_predbat.args.pop(key, None)
        my_predbat.ha_interface.dummy_items.pop(IOG_SENSOR, None)
    print("*** octopus_charge_target test {}".format("FAILED" if failed else "PASSED"))
    return failed
