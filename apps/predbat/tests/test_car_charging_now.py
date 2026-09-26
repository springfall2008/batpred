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
car_charging_now is evidence that the car is drawing power, never a slot.

It holds the battery for the car ("Hold for car", see test_execute.py) and, with dynamic load on,
models the car's load for the rest of the current plan interval - but it never adds a slot to the
published car plan or an Octopus Intelligent dispatch. Those come only from the Predbat car planner
or from Octopus. A slot added from car_charging_now drove binary_sensor.predbat_car_charging_slot,
which an automation uses to start the charger, so the charge kept itself going into peak rates.
"""
import copy

from const import PREDICT_STEP
from prediction import Prediction
from tests.test_infra import reset_inverter

SENSOR = "binary_sensor.car_charging_now_test"

STATE_FIELDS = (
    "num_cars",
    "minutes_now",
    "car_charging_now",
    "car_charging_planned",
    "car_charging_soc",
    "car_charging_limit",
    "car_charging_rate",
    "car_charging_loss",
    "car_charging_plan_smart",
    "car_charging_plan_max_price",
    "car_charging_plan_time",
    "car_charging_slots",
    "car_charging_now_slots",
    "car_charging_from_battery",
    "car_charging_hold",
    "car_charging_energy",
    "car_charging_threshold",
    "car_energy_reported_load",
    "set_charge_window",
    "update_pending",
    "dynamic_load_car_sensors",
    "metric_dynamic_load_adjust",
    "load_last_period",
    "load_last_status",
    "dynamic_load_baseline",
    "battery_rate_max_discharge",
    "low_rates",
    "octopus_slots",
    "octopus_intelligent_charging",
    "car_charging_manual_soc",
    "car_charging_battery_size",
    "car_charging_limit_model",
    "car_charging_soc_next",
    "dispatch_timeline_pending",
)
IOG_SENSOR = "binary_sensor.octopus_intelligent_slot_test"


def _check(name, condition, detail=""):
    """
    Print an error for a failed condition and return whether it failed.
    """
    if not condition:
        print("ERROR: {} {}".format(name, detail))
        return True
    return False


def _car(my_predbat, charging_now):
    """
    A single car, plugged in and with room for charge, reporting charging_now.
    """
    my_predbat.num_cars = 1
    my_predbat.car_charging_now = [charging_now]
    my_predbat.car_charging_planned = [True]
    my_predbat.car_charging_soc = [0.0]
    my_predbat.car_charging_limit = [50.0]
    my_predbat.car_charging_rate = [7.2]
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_plan_smart = [False]
    my_predbat.car_charging_plan_max_price = [0]
    my_predbat.car_charging_plan_time = ["07:00:00"]
    my_predbat.car_charging_slots = [[]]
    my_predbat.car_charging_now_slots = [[]]
    my_predbat.car_charging_from_battery = False
    my_predbat.set_charge_window = True


def _sensor(my_predbat, state):
    """
    Point car_charging_now at a real entity reporting state, or remove it when state is None.
    """
    if state is None:
        my_predbat.args.pop("car_charging_now", None)
    else:
        my_predbat.args["car_charging_now"] = SENSOR
        my_predbat.ha_interface.dummy_items[SENSOR] = state
    my_predbat.dynamic_load_car_refresh_sensors()


def _run_plan(my_predbat):
    """
    The Predbat car planner never adds a slot of its own for a car that is charging now.
    """
    failed = False
    print("Test 1: charging now with no low-rate slots plans nothing")
    _car(my_predbat, True)
    my_predbat.minutes_now = 12 * 60
    plan = my_predbat.plan_car_charging(0, [])
    failed |= _check("t1 no slot", plan == [], "plan {}".format(plan))

    print("Test 2: charging now only plans the low-rate slots")
    low_rates = [{"start": 14 * 60, "end": 15 * 60, "average": 7.0}]
    plan = my_predbat.plan_car_charging(0, low_rates)
    failed |= _check("t2 low-rate slot only", [slot["start"] for slot in plan] == [14 * 60], "plan {}".format(plan))
    return failed


def _run_iog(my_predbat):
    """
    With Octopus Intelligent, a car charging outside any dispatch gets no pretend dispatch - and so
    neither a car slot nor the cheap rate a real dispatch would bring.
    """
    failed = False
    print("Test 17: charging now adds no Octopus Intelligent dispatch")
    _car(my_predbat, True)
    my_predbat.minutes_now = 12 * 60
    my_predbat.octopus_intelligent_charging = True
    my_predbat.octopus_slots = [[]]
    my_predbat.car_charging_manual_soc = [False]
    my_predbat.car_charging_battery_size = [100.0]
    my_predbat.dispatch_timeline_pending = []
    my_predbat.args["octopus_intelligent_slot"] = IOG_SENSOR
    my_predbat.ha_interface.dummy_items[IOG_SENSOR] = "off"
    my_predbat.fetch_sensor_data_cars(save=False)
    failed |= _check("t17 no dispatch", my_predbat.octopus_slots == [[]], "octopus_slots {}".format(my_predbat.octopus_slots))
    failed |= _check("t17 no car slot", my_predbat.car_charging_slots == [[]], "slots {}".format(my_predbat.car_charging_slots))
    return failed


def _run_poll(my_predbat):
    """
    The 15 second loop replans as soon as a car_charging_now entity flips, so the hold follows it.
    """
    failed = False
    _car(my_predbat, False)
    my_predbat.metric_dynamic_load_adjust = False

    print("Test 3: a car that starts charging asks for a replan")
    _sensor(my_predbat, "on")
    my_predbat.update_pending = False
    due = my_predbat.car_charging_now_poll()
    failed |= _check("t3 due", due and my_predbat.update_pending, "due {}".format(due))

    print("Test 4: no change, no replan")
    my_predbat.car_charging_now = [True]
    my_predbat.update_pending = False
    due = my_predbat.car_charging_now_poll()
    failed |= _check("t4 not due", (not due) and not my_predbat.update_pending, "due {}".format(due))

    print("Test 5: a car that stops charging asks for a replan")
    _sensor(my_predbat, "off")
    due = my_predbat.car_charging_now_poll()
    failed |= _check("t5 due", due and my_predbat.update_pending, "due {}".format(due))

    print("Test 6: unavailable is no evidence")
    _sensor(my_predbat, "unavailable")
    my_predbat.update_pending = False
    due = my_predbat.car_charging_now_poll()
    failed |= _check("t6 not due", (not due) and not my_predbat.update_pending, "due {}".format(due))

    print("Test 7: no poll when the car may charge from the battery - there is no hold to apply")
    _sensor(my_predbat, "off")
    my_predbat.car_charging_from_battery = True
    my_predbat.update_pending = False
    due = my_predbat.car_charging_now_poll()
    failed |= _check("t7 not due", (not due) and not my_predbat.update_pending, "due {}".format(due))

    print("Test 7b: ...unless dynamic load models the car's load")
    my_predbat.metric_dynamic_load_adjust = True
    due = my_predbat.car_charging_now_poll()
    failed |= _check("t7b due", due and my_predbat.update_pending, "due {}".format(due))
    my_predbat.metric_dynamic_load_adjust = False
    my_predbat.car_charging_from_battery = False

    print("Test 8: no poll without a real entity")
    _sensor(my_predbat, None)
    my_predbat.update_pending = False
    due = my_predbat.car_charging_now_poll()
    failed |= _check("t8 not due", (not due) and not my_predbat.update_pending, "due {}".format(due))
    return failed


def _dynamic(my_predbat, charging_now, load_kw=1.0, slots=None):
    """
    Run dynamic_load() at 12:10, mid-way through the 12:00-12:30 plan interval.
    """
    reset_inverter(my_predbat)
    _car(my_predbat, charging_now)
    my_predbat.minutes_now = 12 * 60 + 10
    my_predbat.battery_rate_max_discharge = 5.0 / 60.0
    my_predbat.car_charging_threshold = 3.0 / 60.0
    my_predbat.car_energy_reported_load = True
    my_predbat.car_charging_hold = False
    my_predbat.car_charging_energy = None
    my_predbat.load_last_status = "baseline"
    my_predbat.load_last_period = load_kw
    if slots is not None:
        my_predbat.car_charging_slots = [slots]
    my_predbat.dynamic_load()


def _run_dynamic(my_predbat):
    """
    With dynamic load on, a car charging now is modelled for the rest of the interval - for the
    prediction and the export windows only, never in the published car plan.
    """
    failed = False
    now = 12 * 60 + 10
    end = 12 * 60 + 30
    expected_kwh = 7.2 * (end - now) / 60

    print("Test 9: charging now models the car to the end of the interval")
    my_predbat.metric_dynamic_load_adjust = True
    _dynamic(my_predbat, True)
    now_slots = my_predbat.car_charging_now_slots[0]
    failed |= _check("t9 one slot", len(now_slots) == 1, "slots {}".format(now_slots))
    if len(now_slots) == 1:
        failed |= _check("t9 bounds", now_slots[0]["start"] == now and now_slots[0]["end"] == end, "slot {}".format(now_slots[0]))
        failed |= _check("t9 kwh", abs(now_slots[0]["kwh"] - expected_kwh) < 0.001, "slot {}".format(now_slots[0]))
    failed |= _check("t9 published plan untouched", my_predbat.car_charging_slots == [[]], "slots {}".format(my_predbat.car_charging_slots))
    model = my_predbat.car_charging_slots_model()
    failed |= _check("t9 model includes it", len(model[0]) == 1 and model[0][0]["start"] == now, "model {}".format(model))

    print("Test 10: an export window over the charging car is blocked, a later one is not")
    failed |= _check("t10 hit", my_predbat.hit_car_window(now, end + 30), "")
    failed |= _check("t10 miss", not my_predbat.hit_car_window(end + 60, end + 90), "")

    print("Test 11: the live prediction models it, a replay built without it does not")
    step = PREDICT_STEP
    pv_step = {minute: 0.0 for minute in range(0, my_predbat.forecast_minutes, step)}
    load_step = {minute: 0.1 for minute in range(0, my_predbat.forecast_minutes, step)}
    pred = Prediction(my_predbat, pv_step, pv_step, load_step, load_step, car_charging_slots=model)
    failed |= _check("t11 live", pred.car_charging_slots == model, "slots {}".format(pred.car_charging_slots))
    pred = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    failed |= _check("t11 replay", pred.car_charging_slots == [[]], "slots {}".format(pred.car_charging_slots))

    print("Test 12: not charging now, nothing modelled")
    _dynamic(my_predbat, False)
    failed |= _check("t12 none", my_predbat.car_charging_now_slots == [[]], "slots {}".format(my_predbat.car_charging_now_slots))

    print("Test 13: dynamic load off, nothing modelled")
    my_predbat.metric_dynamic_load_adjust = False
    _dynamic(my_predbat, True)
    failed |= _check("t13 none", my_predbat.car_charging_now_slots == [[]], "slots {}".format(my_predbat.car_charging_now_slots))
    failed |= _check("t13 no export block", not my_predbat.hit_car_window(now, end + 30), "")
    my_predbat.metric_dynamic_load_adjust = True

    print("Test 14: a planned slot already covering now models the car, so nothing is added")
    _dynamic(my_predbat, True, slots=[{"start": 12 * 60, "end": 13 * 60, "kwh": 7.2}])
    failed |= _check("t14 none", my_predbat.car_charging_now_slots == [[]], "slots {}".format(my_predbat.car_charging_now_slots))

    print("Test 15: a covering slot with no energy left does not model the car, so it is added")
    _dynamic(my_predbat, True, slots=[{"start": 12 * 60, "end": 13 * 60, "kwh": 0}])
    failed |= _check("t15 added", len(my_predbat.car_charging_now_slots[0]) == 1, "slots {}".format(my_predbat.car_charging_now_slots))

    print("Test 16: the car's load is taken out of the high-load baseline, so it is not counted twice")
    _dynamic(my_predbat, True, load_kw=12.0)
    expected_baseline = (12.0 - 7.2) / 60 * PREDICT_STEP
    values = list(my_predbat.dynamic_load_baseline.values())
    failed |= _check("t16 baseline", values and all(abs(value - expected_baseline) < 0.001 for value in values), "baseline {}".format(my_predbat.dynamic_load_baseline))
    return failed


def test_car_charging_now(my_predbat):
    """
    car_charging_now holds the battery and feeds the model, but never adds a car slot.
    """
    print("*** Running test: car_charging_now")
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in STATE_FIELDS}
    saved_args = {key: copy.deepcopy(my_predbat.args[key]) for key in ("car_charging_now", "octopus_intelligent_slot") if key in my_predbat.args}
    try:
        failed = _run_plan(my_predbat)
        failed |= _run_poll(my_predbat)
        failed |= _run_dynamic(my_predbat)
        failed |= _run_iog(my_predbat)
    finally:
        for field, value in saved_state.items():
            setattr(my_predbat, field, value)
        for key in ("car_charging_now", "octopus_intelligent_slot"):
            if key in saved_args:
                my_predbat.args[key] = saved_args[key]
            else:
                my_predbat.args.pop(key, None)
        my_predbat.ha_interface.dummy_items.pop(SENSOR, None)
        my_predbat.ha_interface.dummy_items.pop(IOG_SENSOR, None)
        my_predbat.dynamic_load_car_refresh_sensors()
    print("*** car_charging_now test {}".format("FAILED" if failed else "PASSED"))
    return failed
