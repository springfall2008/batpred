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
car_charging_max_rate caps each car's planned charge rate at the car's own maximum.

The charging rate (input_number, a charger integration, or Octopus's charger power) is only what
the charger can deliver; a 3.6 kW plug-in hybrid on a 7.4 kW charger was planned at 7.4 kW, so its
charge came out too short. Fetch applies the cap per car where the rate is read, and again after
Octopus's charger power raises it.
"""

import copy
import shutil
import tempfile
from datetime import timedelta

from tests.test_infra import run_async, MockStorageComponents, make_test_storage
from tests.test_multi_car_iog import pin_test_clock, restore_test_clock

CAP_SENSOR = "sensor.car_max_rate_test"
MISSING_SENSOR = "sensor.car_max_rate_test_does_not_exist"
IOG_SENSOR = "binary_sensor.car_max_rate_iog_test"

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
    "car_charging_max_rate",
    "car_charging_max_rate_held",
    "car_charging_max_rate_warned",
    "car_charging_max_rate_bad_warned",
    "car_charging_max_rate_logged",
    "car_charging_max_rate_saved",
    "car_charging_slots",
    "car_charging_exclusive",
    "car_charging_manual_soc",
    "car_charging_soc",
    "car_charging_soc_next",
    "car_charging_loss",
    "car_charging_limit_model",
    "car_charging_planned_response",
    "car_charging_now_response",
    "car_charging_from_battery",
    "octopus_intelligent_charging",
    "octopus_intelligent_ignore_unplugged",
    "octopus_intelligent_consider_full",
    "octopus_slots",
    "octopus_slots_planned",
    "dispatch_timeline_pending",
)
SAVED_ARGS = ("num_cars", "car_charging_max_rate", "octopus_intelligent_slot", "car_charging_loss", "car_charging_soc", "car_charging_limit")
RATE_ITEMS = ("car_charging_rate", "car_charging_rate_1")


def _check(name, condition, detail=""):
    """
    Print an error for a failed condition and return whether it failed.
    """
    if not condition:
        print("ERROR: {} {}".format(name, detail))
        return True
    return False


def _setup(my_predbat, num_cars, cap):
    """
    Configure num_cars cars, each with a 7.4 kW charging rate, and car_charging_max_rate set to cap
    (removed when cap is None).
    """
    my_predbat.num_cars = num_cars
    my_predbat.args["num_cars"] = num_cars
    for name in RATE_ITEMS:
        my_predbat.config_index[name]["value"] = 7.4
    if cap is None:
        my_predbat.args.pop("car_charging_max_rate", None)
    else:
        my_predbat.args["car_charging_max_rate"] = cap


def _rates(my_predbat):
    """
    Run the per-cycle car read and return the resulting car charging rates.
    """
    my_predbat.get_car_charging_planned()
    return list(my_predbat.car_charging_rate)


def _capture_logs(my_predbat):
    """
    Record every log line written through my_predbat.log, returning the list they go into.
    """
    lines = []
    original = my_predbat.log

    def log(message, *args, **kwargs):
        """Record the line, then pass it on."""
        lines.append(str(message))
        return original(message, *args, **kwargs)

    my_predbat.log = log
    return lines


def _run_per_car(my_predbat):
    """
    Each car gets its own cap; absent, blank and 0 entries mean no cap.
    """
    failed = False
    print("Test 1: two cars with different caps are each capped at their own")
    _setup(my_predbat, 2, [3.6, 5.0])
    rates = _rates(my_predbat)
    failed |= _check("t1 two caps", rates == [3.6, 5.0], "rates {}".format(rates))
    failed |= _check("t1 caps stored", my_predbat.car_charging_max_rate == [3.6, 5.0], "caps {}".format(my_predbat.car_charging_max_rate))

    print("Test 2: a cap above the charging rate leaves the rate alone")
    _setup(my_predbat, 2, [11.0, 3.6])
    rates = _rates(my_predbat)
    failed |= _check("t2 cap above rate", rates == [7.4, 3.6], "rates {}".format(rates))

    print("Test 3: car_charging_max_rate not set means no cap")
    _setup(my_predbat, 2, None)
    rates = _rates(my_predbat)
    failed |= _check("t3 absent", rates == [7.4, 7.4], "rates {}".format(rates))

    print("Test 4: a blank entry, 0, a negative number, text or a short list mean no cap for that car")
    for cap, expected in (([None, 3.6], [7.4, 3.6]), ([0, 3.6], [7.4, 3.6]), ([3.6], [3.6, 7.4]), ([-1, "abc"], [7.4, 7.4])):
        _setup(my_predbat, 2, cap)
        rates = _rates(my_predbat)
        failed |= _check("t4 {}".format(cap), rates == expected, "rates {}".format(rates))
    return failed


def _run_sensor(my_predbat):
    """
    An entity pointer is read every cycle; a transient drop-out holds the last cap.
    """
    failed = False
    print("Test 5: an entity pointer is read, and a unit of W is converted to kW")
    _setup(my_predbat, 1, [CAP_SENSOR])
    my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "3.6"
    failed |= _check("t5 sensor", _rates(my_predbat) == [3.6], "rates {}".format(my_predbat.car_charging_rate))
    my_predbat.ha_interface.dummy_items[CAP_SENSOR] = {"state": "3300", "unit_of_measurement": "W"}
    failed |= _check("t5 sensor in W", _rates(my_predbat) == [3.3], "rates {}".format(my_predbat.car_charging_rate))

    print("Test 6: unknown/unavailable holds the last cap rather than dropping it")
    my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "3.6"
    _rates(my_predbat)
    for state in ("unavailable", "unknown", ""):
        my_predbat.ha_interface.dummy_items[CAP_SENSOR] = state
        failed |= _check("t6 {!r} holds".format(state), _rates(my_predbat) == [3.6], "rates {}".format(my_predbat.car_charging_rate))

    print("Test 7: the sensor changing to 0 removes the cap, and a later drop-out does not bring it back")
    my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "0"
    failed |= _check("t7 zero", _rates(my_predbat) == [7.4], "rates {}".format(my_predbat.car_charging_rate))
    my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "unavailable"
    failed |= _check("t7 drop-out after zero", _rates(my_predbat) == [7.4], "rates {}".format(my_predbat.car_charging_rate))

    print("Test 8: a held cap is per car")
    _setup(my_predbat, 2, [CAP_SENSOR, 5.0])
    my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "3.6"
    _rates(my_predbat)
    my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "unavailable"
    failed |= _check("t8 per car", _rates(my_predbat) == [3.6, 5.0], "rates {}".format(my_predbat.car_charging_rate))

    print("Test 9: a pointer to an entity that does not exist warns once and means no cap")
    _setup(my_predbat, 1, [MISSING_SENSOR])
    my_predbat.car_charging_max_rate_held = {0: 3.6}
    lines = _capture_logs(my_predbat)
    try:
        first = _rates(my_predbat)
        second = _rates(my_predbat)
    finally:
        del my_predbat.log
    warnings = [line for line in lines if line.startswith("Warn:") and MISSING_SENSOR in line]
    failed |= _check("t9 no cap", first == [7.4] and second == [7.4], "rates {} {}".format(first, second))
    failed |= _check("t9 warned once", len(warnings) == 1, "warnings {}".format(warnings))

    print("Test 9b: a missing entity that comes back and goes away again warns again")
    lines = _capture_logs(my_predbat)
    try:
        my_predbat.ha_interface.dummy_items[MISSING_SENSOR] = "3.6"
        back = _rates(my_predbat)
        my_predbat.ha_interface.dummy_items.pop(MISSING_SENSOR, None)
        gone = _rates(my_predbat)
    finally:
        del my_predbat.log
        my_predbat.ha_interface.dummy_items.pop(MISSING_SENSOR, None)
    warnings = [line for line in lines if line.startswith("Warn:") and MISSING_SENSOR in line]
    failed |= _check("t9b rates", back == [3.6] and gone == [7.4], "rates {} {}".format(back, gone))
    failed |= _check("t9b warned again", len(warnings) == 1, "warnings {}".format(warnings))
    return failed


def _run_states(my_predbat):
    """
    Every non-numeric sensor state holds the last cap; text other than the transient states warns once per car.
    """
    failed = False
    print("Test 14: 'none' in any case holds the last cap, like unknown/unavailable, without a warning")
    _setup(my_predbat, 1, [CAP_SENSOR])
    my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "3.6"
    _rates(my_predbat)
    lines = _capture_logs(my_predbat)
    try:
        for state in ("none", "None", "NONE"):
            my_predbat.ha_interface.dummy_items[CAP_SENSOR] = state
            failed |= _check("t14 {!r} holds".format(state), _rates(my_predbat) == [3.6], "rates {}".format(my_predbat.car_charging_rate))
    finally:
        del my_predbat.log
    failed |= _check("t14 no warning", not [line for line in lines if line.startswith("Warn:")], "lines {}".format(lines))

    print("Test 15: other text holds the last cap and warns once per car, and again after a good reading")
    lines = _capture_logs(my_predbat)
    try:
        my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "fast"
        first = _rates(my_predbat)
        second = _rates(my_predbat)
        my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "3.6"
        _rates(my_predbat)
        my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "slow"
        third = _rates(my_predbat)
    finally:
        del my_predbat.log
    warnings = [line for line in lines if line.startswith("Warn:") and CAP_SENSOR in line]
    failed |= _check("t15 holds", first == [3.6] and second == [3.6] and third == [3.6], "rates {} {} {}".format(first, second, third))
    failed |= _check("t15 warned once per spell", len(warnings) == 2, "warnings {}".format(warnings))
    return failed


def _run_logging(my_predbat):
    """
    The cap is logged when it changes, not every cycle; a short list logs no out-of-range warning; a single
    value applies to every car.
    """
    failed = False
    print("Test 16: a steady cap is logged once over several cycles, and again when it changes")
    _setup(my_predbat, 1, [3.6])
    my_predbat.car_charging_max_rate_logged = {}
    lines = _capture_logs(my_predbat)
    try:
        for _cycle in range(3):
            _rates(my_predbat)
        steady = [line for line in lines if "car_charging_max_rate" in line]
        my_predbat.args["car_charging_max_rate"] = [3.3]
        _rates(my_predbat)
        _rates(my_predbat)
        changed = [line for line in lines if "car_charging_max_rate" in line]
    finally:
        del my_predbat.log
    failed |= _check("t16 steady logs once", len(steady) == 1, "lines {}".format(steady))
    failed |= _check("t16 change logs once more", len(changed) == 2, "lines {}".format(changed))

    print("Test 17: a list shorter than num_cars logs no out-of-range warning")
    _setup(my_predbat, 2, [3.6])
    lines = _capture_logs(my_predbat)
    try:
        rates = _rates(my_predbat)
    finally:
        del my_predbat.log
    failed |= _check("t17 rates", rates == [3.6, 7.4], "rates {}".format(rates))
    failed |= _check("t17 no out-of-range warning", not [line for line in lines if "Out of range index" in line and "car_charging_max_rate" in line], "lines {}".format(lines))

    print("Test 18: a single entity (not a list) applies to every car")
    _setup(my_predbat, 2, CAP_SENSOR)
    my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "3.6"
    lines = _capture_logs(my_predbat)
    try:
        rates = _rates(my_predbat)
    finally:
        del my_predbat.log
    failed |= _check("t18 every car", rates == [3.6, 3.6], "rates {}".format(rates))
    failed |= _check("t18 no setup warning", not [line for line in lines if "incorrectly setup" in line and "car_charging_max_rate" in line], "lines {}".format(lines))
    return failed


def _run_octopus(my_predbat):
    """
    Octopus's charger power only ever raises the rate, but never past the car's own cap.
    """
    failed = False
    saved_clock = pin_test_clock(my_predbat)
    try:
        for cap, expected in (([3.6], 3.6), (None, 7.4)):
            print("Test 10: Octopus's charger power with car_charging_max_rate {}".format(cap))
            _setup(my_predbat, 1, cap)
            my_predbat.config_index["car_charging_rate"]["value"] = 3.0
            my_predbat.get_car_charging_planned()
            my_predbat.car_charging_manual_soc = [False]
            my_predbat.octopus_intelligent_charging = True
            my_predbat.octopus_intelligent_ignore_unplugged = False
            my_predbat.octopus_intelligent_consider_full = False
            my_predbat.octopus_slots = [[]]
            my_predbat.args["car_charging_loss"] = 0.0
            my_predbat.args["car_charging_soc"] = [50.0]
            my_predbat.args["car_charging_limit"] = [100.0]
            my_predbat.args["octopus_intelligent_slot"] = [IOG_SENSOR]
            slot_start = (my_predbat.now_utc + timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S%z")
            slot_end = (my_predbat.now_utc + timedelta(minutes=120)).strftime("%Y-%m-%dT%H:%M:%S%z")
            my_predbat.ha_interface.set_state(
                IOG_SENSOR,
                "off",
                attributes={
                    "completed_dispatches": [],
                    "planned_dispatches": [{"start": slot_start, "end": slot_end, "charge_in_kwh": 3.0, "source": "smart-charge", "location": "AT_HOME"}],
                    "vehicle_battery_size_in_kwh": 60.0,
                    "charge_point_power_in_kw": 7.4,
                },
            )
            lines = _capture_logs(my_predbat)
            try:
                my_predbat.fetch_sensor_data_cars(save=False)
            finally:
                del my_predbat.log
            failed |= _check("t10 cap {}".format(cap), my_predbat.car_charging_rate[0] == expected, "rate {}".format(my_predbat.car_charging_rate))
            failed |= _check("t10 Octopus re-cap logs nothing", not [line for line in lines if "car_charging_max_rate" in line], "lines {}".format(lines))
    finally:
        restore_test_clock(my_predbat, saved_clock)
    return failed


def _run_validation(my_predbat):
    """
    apps.yaml validation accepts what the runtime accepts, and catches a missing entity.
    """
    failed = False

    def errors_for(value, states=None):
        """Validate car_charging_max_rate set to value with 2 cars, returning its error or None."""
        saved_args = copy.deepcopy(my_predbat.args)
        saved_states = my_predbat.ha_interface.dummy_items.copy()
        try:
            my_predbat.args["num_cars"] = 2
            my_predbat.args["car_charging_max_rate"] = value
            my_predbat.ha_interface.dummy_items.update(states or {})
            my_predbat.validate_config()
            return my_predbat.arg_errors.get("car_charging_max_rate")
        finally:
            my_predbat.args.clear()
            my_predbat.args.update(saved_args)
            my_predbat.ha_interface.dummy_items = saved_states

    print("Test 11: validation accepts a mixed list with a blank entry, which the runtime reads as no cap")
    error = errors_for([3.6, None])
    failed |= _check("t11 blank entry validates", error is None, "error {}".format(error))
    _setup(my_predbat, 2, [3.6, None])
    lines = _capture_logs(my_predbat)
    try:
        rates = _rates(my_predbat)
    finally:
        del my_predbat.log
    failed |= _check("t11 blank entry runtime", rates == [3.6, 7.4], "rates {}".format(rates))
    failed |= _check("t11 blank entry no warning", not [line for line in lines if "car_charging_max_rate" in line and line.startswith("Warn")], "lines {}".format(lines))

    print("Test 12: validation accepts numbers, a short list, and a sensor that is unavailable")
    for value, states in (([3.6, 7], None), ([3.6], None), ([CAP_SENSOR, 0], {CAP_SENSOR: "unavailable"}), ([CAP_SENSOR], {CAP_SENSOR: "3.6"})):
        error = errors_for(value, states)
        failed |= _check("t12 {}".format(value), error is None, "error {}".format(error))

    print("Test 13: validation reports a pointer to an entity that does not exist, and a non-number")
    for value, states in (([MISSING_SENSOR, 3.6], None), ([CAP_SENSOR], {CAP_SENSOR: "fast"})):
        error = errors_for(value, states)
        failed |= _check("t13 {}".format(value), error is not None, "no error")
    return failed


def _restart(my_predbat):
    """
    Lose the in-memory cap state, as a restart does, then run the start-up step that comes before the components start.
    """
    my_predbat.car_charging_max_rate = []
    my_predbat.car_charging_max_rate_held = {}
    my_predbat.car_charging_max_rate_saved = None
    my_predbat.car_max_rate_startup()


def _run_restart(my_predbat):
    """
    A held cap is saved to storage when it changes and restored at start-up, and every car's cap is worked
    out at start-up, before the components (Load ML among them) start.
    """
    failed = False
    tmpdir = tempfile.mkdtemp()
    try:
        storage = make_test_storage(my_predbat, tmpdir)
        saves = []
        backend_save = storage.backend.save

        async def counting_save(module, filename, data, **kwargs):
            """Count the writes, then make them."""
            saves.append(filename)
            return await backend_save(module, filename, data, **kwargs)

        storage.backend.save = counting_save
        my_predbat.components = MockStorageComponents(storage)

        print("Test 19: a held cap survives a restart while its sensor is still unavailable")
        _setup(my_predbat, 2, [CAP_SENSOR, 5.0])
        my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "3.6"
        _rates(my_predbat)
        my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "unavailable"
        _restart(my_predbat)
        failed |= _check("t19 caps known at start-up", my_predbat.car_charging_max_rate == [3.6, 5.0], "caps {}".format(my_predbat.car_charging_max_rate))
        failed |= _check("t19 first cycle capped", _rates(my_predbat) == [3.6, 5.0], "rates {}".format(my_predbat.car_charging_rate))

        print("Test 20: the cap is written once while it is steady, and again when it changes")
        saves.clear()
        my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "3.6"
        for _cycle in range(3):
            _rates(my_predbat)
        steady = len(saves)
        my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "3.3"
        _rates(my_predbat)
        _rates(my_predbat)
        failed |= _check("t20 steady writes nothing", steady == 0, "saves {}".format(steady))
        failed |= _check("t20 change writes once", len(saves) == 1, "saves {}".format(saves))

        print("Test 21: a cap removed (sensor reads 0) stays removed after a restart")
        my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "0"
        _rates(my_predbat)
        my_predbat.ha_interface.dummy_items[CAP_SENSOR] = "unavailable"
        _restart(my_predbat)
        failed |= _check("t21 no cap", _rates(my_predbat) == [7.4, 5.0], "rates {}".format(my_predbat.car_charging_rate))

        print("Test 22: junk in storage is ignored")
        for junk in (["not", "a", "dict"], {"held": {"x": 3.6, "0": "fast", "1": -1}}):
            run_async(storage.save("predbat", "car_max_rate", junk, format="json"))
            _restart(my_predbat)
            failed |= _check("t22 {} ignored".format(junk), 0 not in my_predbat.car_charging_max_rate_held and _rates(my_predbat) == [7.4, 5.0], "held {} rates {}".format(my_predbat.car_charging_max_rate_held, my_predbat.car_charging_rate))

        print("Test 23: with no storage, start-up still works out the caps")
        for components in (None, MockStorageComponents(None)):
            my_predbat.components = components
            _setup(my_predbat, 1, [3.6])
            _restart(my_predbat)
            failed |= _check("t23 cap", my_predbat.car_charging_max_rate == [3.6], "caps {}".format(my_predbat.car_charging_max_rate))
            _rates(my_predbat)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return failed


def run_car_max_rate_tests(my_predbat):
    """
    car_charging_max_rate caps each car's charge rate, per car and against Octopus's raise.
    """
    print("*** Running test: car_max_rate")
    saved_state = {field: copy.deepcopy(getattr(my_predbat, field, None)) for field in STATE_FIELDS}
    saved_args = {key: copy.deepcopy(my_predbat.args[key]) for key in SAVED_ARGS if key in my_predbat.args}
    saved_rates = {name: my_predbat.config_index[name].get("value") for name in RATE_ITEMS}
    saved_states = my_predbat.ha_interface.dummy_items.copy()
    saved_components = my_predbat.components
    failed = False
    try:
        failed |= _run_per_car(my_predbat)
        failed |= _run_sensor(my_predbat)
        failed |= _run_states(my_predbat)
        failed |= _run_logging(my_predbat)
        failed |= _run_octopus(my_predbat)
        failed |= _run_validation(my_predbat)
        failed |= _run_restart(my_predbat)
    finally:
        my_predbat.components = saved_components
        for field, value in saved_state.items():
            setattr(my_predbat, field, value)
        for key in SAVED_ARGS:
            if key in saved_args:
                my_predbat.args[key] = saved_args[key]
            else:
                my_predbat.args.pop(key, None)
        for name, value in saved_rates.items():
            my_predbat.config_index[name]["value"] = value
        my_predbat.ha_interface.dummy_items = saved_states
    if failed:
        print("**** car_max_rate tests FAILED ****")
    return failed
