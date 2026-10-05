# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import copy
from datetime import timedelta

from tests.test_multi_car_iog import pin_test_clock, restore_test_clock

SLOT_SENSOR = "binary_sensor.octopus_energy_abc123_intelligent_dispatching"
SWITCH = "switch.octopus_energy_abc123_intelligent_smart_charge"
CAR2_SENSOR = "binary_sensor.octopus_energy_def456_intelligent_dispatching"
CAR2_SWITCH = "switch.octopus_energy_def456_intelligent_smart_charge"
CUSTOM_SENSOR = "binary_sensor.my_car_dispatching"
CUSTOM_SWITCH = "switch.my_car_smart_control"

STATE_FIELDS = (
    "num_cars",
    "car_charging_planned",
    "car_charging_now",
    "car_charging_plan_smart",
    "car_charging_plan_max_price",
    "car_charging_plan_time",
    "car_charging_battery_size",
    "car_charging_limit",
    "car_charging_rate",
    "car_charging_slots",
    "car_charging_exclusive",
    "car_charging_manual_soc",
    "octopus_intelligent_charging",
    "octopus_intelligent_ignore_unplugged",
    "octopus_intelligent_consider_full",
    "octopus_slots",
    "octopus_smart_control_off_logged",
    "dispatch_timeline_pending",
    "dispatch_timeline_last",
    "car_charging_soc",
    "car_charging_soc_next",
    "car_charging_loss",
)
ARG_KEYS = ("car_charging_loss", "car_charging_soc", "car_charging_limit", "octopus_intelligent_slot", "octopus_intelligent_smart_control")


def _setup(my_predbat):
    """
    One IOG car, plugged in, with a planned and a completed dispatch on the slot sensor.
    """
    my_predbat.num_cars = 1
    my_predbat.car_charging_planned = [True]
    my_predbat.car_charging_now = [False]
    my_predbat.car_charging_plan_smart = [False]
    my_predbat.car_charging_plan_max_price = [0]
    my_predbat.car_charging_plan_time = ["07:00:00"]
    my_predbat.car_charging_battery_size = [100.0]
    my_predbat.car_charging_limit = [100.0]
    my_predbat.car_charging_rate = [7.4]
    my_predbat.car_charging_slots = [[]]
    my_predbat.car_charging_exclusive = [False]
    my_predbat.car_charging_manual_soc = [False]
    my_predbat.octopus_intelligent_charging = True
    my_predbat.octopus_intelligent_ignore_unplugged = False
    my_predbat.octopus_intelligent_consider_full = False
    my_predbat.octopus_smart_control_off_logged = {}
    my_predbat.args["car_charging_loss"] = 0.0
    my_predbat.args["car_charging_soc"] = [50.0]
    my_predbat.args["car_charging_limit"] = [100.0]
    now = my_predbat.now_utc
    fmt = "%Y-%m-%dT%H:%M:%S%z"
    attributes = {
        "completed_dispatches": [{"start": (now - timedelta(hours=3)).strftime(fmt), "end": (now - timedelta(hours=2)).strftime(fmt), "charge_in_kwh": 5.0, "source": "smart-charge", "location": "AT_HOME"}],
        "planned_dispatches": [{"start": (now + timedelta(hours=1)).strftime(fmt), "end": (now + timedelta(hours=2)).strftime(fmt), "charge_in_kwh": 10.0, "source": "smart-charge", "location": "AT_HOME"}],
        "vehicle_battery_size_in_kwh": 100.0,
        "charge_point_power_in_kw": 7.4,
    }
    my_predbat.ha_interface.set_state(SLOT_SENSOR, "off", attributes=copy.deepcopy(attributes))
    my_predbat.ha_interface.set_state(CUSTOM_SENSOR, "off", attributes=copy.deepcopy(attributes))
    my_predbat.ha_interface.set_state(CAR2_SENSOR, "off", attributes=copy.deepcopy(attributes))
    my_predbat.args["octopus_intelligent_slot"] = SLOT_SENSOR
    my_predbat.args["octopus_intelligent_smart_control"] = SWITCH


def _slots(my_predbat, save=False, car_n=0):
    """
    Run the fetch and return the dispatches' own kWh it produced for car_n, sorted - 5.0 is the completed one, 10.0 the planned one.
    """
    my_predbat.octopus_slots = [[] for _ in range(my_predbat.num_cars)]
    my_predbat.fetch_sensor_data_cars(save=save)
    return sorted(slot.get("charge_in_kwh") for slot in my_predbat.octopus_slots[car_n])


def _check(name, condition, detail=""):
    """
    Print an error for a failed condition and return whether it failed.
    """
    if not condition:
        print("ERROR: {} {}".format(name, detail))
        return True
    return False


def run_octopus_smart_control_tests(my_predbat):
    """
    Octopus keeps returning its plan after Smart Control is switched off, so planned dispatches must be
    ignored while the Smart Control switch is off, and completed ones kept (#5339).
    """
    print("*** Running test: octopus_smart_control")
    failed = False
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in STATE_FIELDS}
    saved_args = {key: copy.deepcopy(my_predbat.args[key]) for key in ARG_KEYS if key in my_predbat.args}
    saved_clock = pin_test_clock(my_predbat)
    items = my_predbat.ha_interface.dummy_items
    try:
        _setup(my_predbat)

        print("Test 1: no switch entity - planned and completed slots both used")
        items.pop(SWITCH, None)
        kwh = _slots(my_predbat)
        failed |= _check("t1 both", kwh == [5.0, 10.0], "kwh {}".format(kwh))

        print("Test 2: switch on - planned and completed slots both used")
        items[SWITCH] = "on"
        kwh = _slots(my_predbat)
        failed |= _check("t2 both", kwh == [5.0, 10.0], "kwh {}".format(kwh))

        print("Test 3: switch off - planned slots dropped, completed kept")
        items[SWITCH] = "off"
        kwh = _slots(my_predbat)
        failed |= _check("t3 completed only", kwh == [5.0], "kwh {}".format(kwh))

        print("Test 4: switch back on - planned slots return")
        items[SWITCH] = "on"
        kwh = _slots(my_predbat)
        failed |= _check("t4 both again", kwh == [5.0, 10.0], "kwh {}".format(kwh))

        print("Test 5: unavailable or unknown switch is no evidence - planned slots kept")
        for state in ("unavailable", "unknown", None):
            items[SWITCH] = state
            kwh = _slots(my_predbat)
            failed |= _check("t5 {} kept".format(state), kwh == [5.0, 10.0], "kwh {}".format(kwh))

        print("Test 6: with octopus_intelligent_smart_control not set nothing is guessed from the sensor name")
        items[SWITCH] = "off"
        my_predbat.args.pop("octopus_intelligent_smart_control", None)
        kwh = _slots(my_predbat)
        failed |= _check("t6 not guessed", kwh == [5.0, 10.0], "kwh {}".format(kwh))

        print("Test 7: octopus_intelligent_smart_control as a single value")
        items[CUSTOM_SWITCH] = "off"
        my_predbat.args["octopus_intelligent_smart_control"] = CUSTOM_SWITCH
        kwh = _slots(my_predbat)
        failed |= _check("t7 explicit off", kwh == [5.0], "kwh {}".format(kwh))
        items[CUSTOM_SWITCH] = "on"
        kwh = _slots(my_predbat)
        failed |= _check("t7 explicit on", kwh == [5.0, 10.0], "kwh {}".format(kwh))

        print("Test 8: octopus_intelligent_smart_control as a list reads only its own switch")
        my_predbat.args["octopus_intelligent_smart_control"] = [CUSTOM_SWITCH]
        items[SWITCH] = "off"
        items[CUSTOM_SWITCH] = "on"
        kwh = _slots(my_predbat)
        failed |= _check("t8 own switch read", kwh == [5.0, 10.0], "kwh {}".format(kwh))

        print("Test 9: the slot signature changes when Smart Control goes off, so the plan is recomputed")
        my_predbat.args["octopus_intelligent_smart_control"] = SWITCH
        items[SWITCH] = "on"
        _slots(my_predbat)
        before = copy.deepcopy(my_predbat.octopus_slots)
        items[SWITCH] = "off"
        _slots(my_predbat)
        failed |= _check("t9 signature", my_predbat.octopus_slots_signature(before) != my_predbat.octopus_slots_signature(my_predbat.octopus_slots), "")

        print("Test 10: manual bump/boost dispatches are kept while Smart Control is off, scheduled ones are not")
        my_predbat.num_cars = 1
        my_predbat.args["octopus_intelligent_slot"] = SLOT_SENSOR
        my_predbat.args["octopus_intelligent_smart_control"] = SWITCH
        attributes = copy.deepcopy(items[SLOT_SENSOR])
        boost_start = (my_predbat.now_utc + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S%z")
        boost_end = (my_predbat.now_utc + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S%z")
        attributes["planned_dispatches"].append({"start": boost_start, "end": boost_end, "charge_in_kwh": 3.0, "source": "bump-charge", "location": "AT_HOME"})
        attributes["planned_dispatches"].append({"start": boost_start, "end": boost_end, "charge_in_kwh": 2.0, "source": "BOOST", "location": "AT_HOME"})
        my_predbat.ha_interface.set_state(SLOT_SENSOR, "off", attributes=attributes)
        items[SWITCH] = "off"
        kwh = _slots(my_predbat)
        failed |= _check("t10 boost kept", kwh == [2.0, 3.0, 5.0], "kwh {}".format(kwh))
        items[SWITCH] = "on"
        kwh = _slots(my_predbat)
        failed |= _check("t10 all on", kwh == [2.0, 3.0, 5.0, 10.0], "kwh {}".format(kwh))
        _setup(my_predbat)

        print("Test 11: the state is case-insensitive, and an odd state string is no evidence")
        my_predbat.args["octopus_intelligent_slot"] = SLOT_SENSOR
        items[SWITCH] = "OFF"
        kwh = _slots(my_predbat)
        failed |= _check("t11 OFF", kwh == [5.0], "kwh {}".format(kwh))
        items[SWITCH] = "true"
        kwh = _slots(my_predbat)
        failed |= _check("t11 true", kwh == [5.0, 10.0], "kwh {}".format(kwh))

        print("Test 12: the change is logged once on a live fetch, and not at all on a save=False re-run")
        logs = []
        real_log = my_predbat.log
        my_predbat.log = lambda message, *args, **kwargs: (logs.append(message), real_log(message, *args, **kwargs))[1]
        try:
            items[SWITCH] = "off"
            my_predbat.octopus_smart_control_off_logged = {}
            _slots(my_predbat, save=False)
            failed |= _check("t12 no log on re-run", not [x for x in logs if "Smart Control is now" in x], "logs {}".format(logs))
            _slots(my_predbat, save=True)
            _slots(my_predbat, save=True)
            now_off = [x for x in logs if "Smart Control is now Off" in x]
            failed |= _check("t12 logged once", len(now_off) == 1, "logs {}".format(logs))
            items[SWITCH] = "unavailable"
            _slots(my_predbat, save=True)
            no_longer = [x for x in logs if "Smart Control is no longer Off" in x]
            failed |= _check("t12 unavailable is not called On", len(no_longer) == 1 and "is unavailable" in no_longer[0] and not [x for x in logs if "Smart Control is now On" in x], "logs {}".format(logs))
        finally:
            my_predbat.log = real_log

        print("Test 13: two cars - Smart Control off for one only")
        my_predbat.num_cars = 2
        my_predbat.car_charging_planned = [True, True]
        my_predbat.car_charging_now = [False, False]
        my_predbat.car_charging_plan_smart = [False, False]
        my_predbat.car_charging_plan_max_price = [0, 0]
        my_predbat.car_charging_plan_time = ["07:00:00", "07:00:00"]
        my_predbat.car_charging_battery_size = [100.0, 100.0]
        my_predbat.car_charging_limit = [100.0, 100.0]
        my_predbat.car_charging_rate = [7.4, 7.4]
        my_predbat.car_charging_slots = [[], []]
        my_predbat.car_charging_exclusive = [False, False]
        my_predbat.car_charging_manual_soc = [False, False]
        my_predbat.args["car_charging_soc"] = [50.0, 50.0]
        my_predbat.args["car_charging_limit"] = [100.0, 100.0]
        my_predbat.args["octopus_intelligent_slot"] = [SLOT_SENSOR, CAR2_SENSOR]
        my_predbat.args["octopus_intelligent_smart_control"] = [SWITCH, CAR2_SWITCH]
        items[SWITCH] = "off"
        items[CAR2_SWITCH] = "on"
        failed |= _check("t13 car 0 off", _slots(my_predbat, car_n=0) == [5.0], "")
        failed |= _check("t13 car 1 on", sorted(slot.get("charge_in_kwh") for slot in my_predbat.octopus_slots[1]) == [5.0, 10.0], "")
        items[SWITCH] = "on"
        items[CAR2_SWITCH] = "off"
        _slots(my_predbat)
        failed |= _check("t13 car 0 on", sorted(slot.get("charge_in_kwh") for slot in my_predbat.octopus_slots[0]) == [5.0, 10.0], "")
        failed |= _check("t13 car 1 off", sorted(slot.get("charge_in_kwh") for slot in my_predbat.octopus_slots[1]) == [5.0], "")

        print("Test 14: a switch list shorter than the cars leaves the extra cars' planned slots trusted")
        my_predbat.args["octopus_intelligent_smart_control"] = [CUSTOM_SWITCH]
        items[CUSTOM_SWITCH] = "on"
        _slots(my_predbat)
        failed |= _check("t14 car 0 switch on", sorted(slot.get("charge_in_kwh") for slot in my_predbat.octopus_slots[0]) == [5.0, 10.0], "")
        failed |= _check("t14 car 1 no switch", sorted(slot.get("charge_in_kwh") for slot in my_predbat.octopus_slots[1]) == [5.0, 10.0], "")
    finally:
        for field, value in saved_state.items():
            setattr(my_predbat, field, value)
        for key in ARG_KEYS:
            if key in saved_args:
                my_predbat.args[key] = saved_args[key]
            else:
                my_predbat.args.pop(key, None)
        for entity in (SLOT_SENSOR, CUSTOM_SENSOR, CAR2_SENSOR, SWITCH, CUSTOM_SWITCH, CAR2_SWITCH):
            items.pop(entity, None)
        restore_test_clock(my_predbat, saved_clock)
    print("*** octopus_smart_control test {}".format("FAILED" if failed else "PASSED"))
    return failed
