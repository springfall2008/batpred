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
Integration guards between the manual car SoC handling of v9.4.6 and the combined planning work.

The combined recurring session manages car 0's SoC itself. Three things must hold when it is active:

- The manual SoC of a car is read through car_manual_soc_kwh(), which clamps the value to the car's
  battery size, and not as the raw configured number the combined branch read.
- The provisional SoC the combined session holds for car 0 is never mirrored into the manual SoC
  entity: that would publish a placeholder over the user's real value.
- The Octopus battery size update only takes the manual branch when the car really is tracked
  manually. The combined-managed car 0 must keep its provisional value.
"""

from tests.car_planning_fixtures import clear_car_settings, configure_car_settings

from car_charging_recurring import recurring_enabled

SOC_SENSOR = "sensor.car_manual_soc_planning_test_soc"
IOG_SENSOR = "binary_sensor.car_manual_soc_planning_test_iog_slot"


def _check(name, condition, detail=""):
    """
    Print an error for a failed condition and return whether it failed.
    """
    if not condition:
        print("ERROR: {} {}".format(name, detail))
        return True
    return False


def _car_planning_settings():
    """
    A car_charging_solver configuration with the recurring session enabled, as the combined branch recognises it.
    """
    return {
        "mode": "apply",
        "recurring_session": {
            "enabled": True,
        },
    }


def _one_car(my_predbat):
    """
    A single car with a SoC sensor, a 60 kWh battery and no manual tracking.
    """
    my_predbat.args.pop("car_charging_manual_soc", None)
    my_predbat.args.pop("car_charging_soc", None)
    my_predbat.args.pop("car_charging_planned", None)
    my_predbat.args.pop("car_charging_plugged", None)
    my_predbat.args.pop("car_charging_energy", None)
    my_predbat.args.pop("car_charging_now", None)
    my_predbat.args.update({"car_charging_soc": SOC_SENSOR})
    my_predbat.num_cars = 1
    my_predbat.car_charging_loss = 1.0
    my_predbat.car_charging_energy_scale = 1.0
    my_predbat.car_charging_battery_size = [60.0]
    my_predbat.car_charging_limit = [60.0]
    my_predbat.car_charging_rate = [7.4]
    my_predbat.car_charging_now = [False]
    my_predbat.car_charging_soc = [0.0]
    my_predbat.car_charging_soc_next = [None]
    my_predbat.car_manual_soc_state = {}
    my_predbat.expose_config("car_charging_manual_soc", False)
    my_predbat.expose_config("car_charging_manual_soc_kwh", 0.0)
    my_predbat.car_manual_soc_config()


def _run_car_planning_detection(my_predbat):
    """
    The combined settings are recognised only when the recurring session is explicitly enabled.
    """
    failed = False
    print("Test 1: an enabled recurring car session is recognised")
    failed |= _check("j1 enabled", recurring_enabled(_car_planning_settings()) is True, "not recognised")
    print("Test 2: a disabled recurring car session is not selected")
    failed |= _check("j2 disabled", recurring_enabled({"mode": "apply"}) is False, "recognised without a session")
    failed |= _check("j2 not a dict", recurring_enabled(None) is False, "recognised without settings")
    return failed


def _run_mirror_guard(my_predbat):
    """
    The provisional SoC of a combined-managed car 0 is not published as the manual SoC.
    """
    failed = False
    dummy = my_predbat.ha_interface.dummy_items

    print("Test J3: a car that is not combined-managed mirrors its sensor SoC into the manual entity")
    _one_car(my_predbat)
    dummy[SOC_SENSOR] = 50.0
    my_predbat.car_charging_soc = [30.0]
    my_predbat.car_manual_soc_mirror()
    failed |= _check("j3 mirrored", abs(my_predbat.get_arg("car_charging_manual_soc_kwh", 0.0) - 30.0) < 0.001, "kwh {}".format(my_predbat.get_arg("car_charging_manual_soc_kwh", 0.0)))

    print("Test J4: the same car is left alone when the combined session manages it")
    my_predbat.expose_config("car_charging_manual_soc_kwh", 12.5)
    my_predbat.car_charging_soc = [0.0]
    my_predbat.car_manual_soc_mirror(managed_cars={0})
    failed |= _check("j4 untouched", abs(my_predbat.get_arg("car_charging_manual_soc_kwh", 0.0) - 12.5) < 0.001, "kwh {}".format(my_predbat.get_arg("car_charging_manual_soc_kwh", 0.0)))

    print("Test J5: a car the combined session does not manage is still mirrored")
    my_predbat.car_charging_soc = [7.5]
    my_predbat.car_manual_soc_mirror(managed_cars=set())
    failed |= _check("j5 mirrored", abs(my_predbat.get_arg("car_charging_manual_soc_kwh", 0.0) - 7.5) < 0.001, "kwh {}".format(my_predbat.get_arg("car_charging_manual_soc_kwh", 0.0)))
    return failed


def _run_clamped_manual_soc(my_predbat):
    """
    The manual SoC is read through car_manual_soc_kwh(), so a value above the battery size is clamped.
    """
    failed = False
    _one_car(my_predbat)
    my_predbat.car_charging_battery_size = [60.0]
    print("Test J6: a manual SoC above the battery size is clamped by car_manual_soc_kwh()")
    my_predbat.expose_config("car_charging_manual_soc_kwh", 90.0)
    failed |= _check("j6 clamped", abs(my_predbat.car_manual_soc_kwh(0) - 60.0) < 0.001, "soc {}".format(my_predbat.car_manual_soc_kwh(0)))
    my_predbat.expose_config("car_charging_manual_soc_kwh", -5.0)
    failed |= _check("j6 floored", abs(my_predbat.car_manual_soc_kwh(0)) < 0.001, "soc {}".format(my_predbat.car_manual_soc_kwh(0)))
    return failed


def _run_battery_size_update(my_predbat):
    """
    The Octopus battery size update only takes the manual branch for a car that really is tracked
    manually. A car the combined session manages keeps its provisional SoC.
    """
    failed = False
    dummy = my_predbat.ha_interface.dummy_items

    print("Test J7: the Octopus battery size update clamps a manually tracked car")
    _one_car(my_predbat)
    my_predbat.expose_config("car_charging_manual_soc", True)
    my_predbat.car_manual_soc_config()
    my_predbat.expose_config("car_charging_manual_soc_kwh", 90.0)
    my_predbat.car_charging_soc = [0.0]
    my_predbat.args["octopus_intelligent_slot"] = IOG_SENSOR
    dummy[IOG_SENSOR] = {"state": "off", "vehicle_battery_size_in_kwh": 75.0, "completed_dispatches": [], "planned_dispatches": []}
    my_predbat.fetch_sensor_data_cars(save=True)
    failed |= _check("j7 clamped to octopus size", abs(my_predbat.car_charging_soc[0] - 75.0) < 0.001, "soc {}".format(my_predbat.car_charging_soc))

    print("Test J8: a car the combined session manages is not given the manual branch")
    my_predbat.expose_config("car_charging_manual_soc", False)
    my_predbat.car_manual_soc_config()
    configure_car_settings(my_predbat, _car_planning_settings())
    my_predbat.car_charging_soc = [0.0]
    my_predbat.fetch_sensor_data_cars(save=True)
    failed |= _check("j8 provisional kept", abs(my_predbat.car_charging_soc[0]) < 0.001, "soc {}".format(my_predbat.car_charging_soc))
    failed |= _check("j8 size updated", abs(my_predbat.car_charging_battery_size[0] - 75.0) < 0.001, "size {}".format(my_predbat.car_charging_battery_size))

    clear_car_settings(my_predbat)
    my_predbat.args.pop("octopus_intelligent_slot", None)
    return failed


def run_car_manual_soc_car_planning_tests(my_predbat):
    """
    Run the combined integration guards for the manual car SoC.
    """
    failed = False
    failed |= _run_car_planning_detection(my_predbat)
    failed |= _run_mirror_guard(my_predbat)
    failed |= _run_clamped_manual_soc(my_predbat)
    failed |= _run_battery_size_update(my_predbat)
    return failed


def test_car_manual_soc_car_planning():
    """
    Entry point used by the unit test runner.
    """
    from unit_test import create_predbat

    return run_car_manual_soc_car_planning_tests(create_predbat())
