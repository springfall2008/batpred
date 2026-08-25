# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from const import EXPORT_LIMIT_FREEZE, EXPORT_LIMIT_IDLE
from tests.test_infra import reset_inverter

SURPLUS_ENTITY = ".solar_surplus_power"
EXPORT_SLOT_ENTITY = "_force_export_slot"


def check_surplus(my_predbat, name, grid_power, battery_power, car_charging_power, car_configured, expect_state):
    """Publish the inverter sensors for one power reading and check the surplus state"""
    my_predbat.grid_power = grid_power
    my_predbat.battery_power = battery_power
    my_predbat.car_charging_power = car_charging_power
    my_predbat.car_charging_power_configured = car_configured
    my_predbat.publish_inverter_data()

    entity_id = my_predbat.prefix + SURPLUS_ENTITY
    item = my_predbat.ha_interface.dummy_items.get(entity_id)
    if item is None:
        print("ERROR: {} was not published for {}".format(entity_id, name))
        return True
    if item.get("state", None) != expect_state:
        print("ERROR: {} state is {} expected {} for {}".format(entity_id, item.get("state", None), expect_state, name))
        return True
    return False


def check_export_slot(my_predbat, name, minutes_now, export_window, export_limits, set_export_window, expect_on):
    """Publish the charge/export status for one plan state and check the force export slot sensor"""
    my_predbat.minutes_now = minutes_now
    my_predbat.export_window_best = export_window
    my_predbat.export_limits_best = export_limits
    my_predbat.set_export_window = set_export_window
    my_predbat.set_charge_export_status(False, False, True)

    entity_id = "binary_sensor." + my_predbat.prefix + EXPORT_SLOT_ENTITY
    item = my_predbat.ha_interface.dummy_items.get(entity_id)
    if item is None:
        print("ERROR: {} was not published for {}".format(entity_id, name))
        return True
    expect_state = "on" if expect_on else "off"
    if item.get("state", None) != expect_state:
        print("ERROR: {} state is {} expected {} for {}".format(entity_id, item.get("state", None), expect_state, name))
        return True
    return False


def run_export_slot_tests(my_predbat):
    """
    binary_sensor.predbat_force_export_slot reports whether the plan has a force export window
    running now, so an automation can leave the solar for Predbat to sell rather than putting it
    into the car.
    """
    failed = False
    saved = {key: getattr(my_predbat, key) for key in ["minutes_now", "export_window_best", "export_limits_best", "set_export_window"]}
    window = [{"start": 600, "end": 660}]

    try:
        print("Test: inside a force export window the sensor is on")
        failed |= check_export_slot(my_predbat, "inside window", 630, window, [10.0], True, True)

        print("Test: a freeze export window counts, as Predbat is selling the solar")
        failed |= check_export_slot(my_predbat, "freeze export", 630, window, [EXPORT_LIMIT_FREEZE], True, True)

        print("Test: a window left at the idle limit is not an export slot")
        failed |= check_export_slot(my_predbat, "idle limit", 630, window, [EXPORT_LIMIT_IDLE], True, False)

        print("Test: before and after the window the sensor is off")
        failed |= check_export_slot(my_predbat, "before window", 599, window, [10.0], True, False)
        failed |= check_export_slot(my_predbat, "after window", 660, window, [10.0], True, False)

        print("Test: with export control disabled the sensor is off even inside a window")
        failed |= check_export_slot(my_predbat, "export control off", 630, window, [10.0], False, False)

        print("Test: with no windows planned the sensor is off")
        failed |= check_export_slot(my_predbat, "no windows", 630, [], [], True, False)

        print("Test: the charging/exporting/demand sensors still report the commanded state")
        my_predbat.set_charge_export_status(True, False, False)
        for entity, expect_state in [("_charging", "on"), ("_exporting", "off"), ("_demand", "off")]:
            entity_id = "binary_sensor." + my_predbat.prefix + entity
            item = my_predbat.ha_interface.dummy_items.get(entity_id)
            if item is None or item.get("state", None) != expect_state:
                print("ERROR: {} state is {} expected {}".format(entity_id, item.get("state", None) if item else None, expect_state))
                failed = True
    finally:
        for key, value in saved.items():
            setattr(my_predbat, key, value)

    return failed


def run_solar_surplus_tests(my_predbat):
    """
    predbat.solar_surplus_power reports the power a flexible load could take right now without
    importing or draining the battery, so a Home Assistant automation can start a car, an immersion
    heater or anything else on spare solar without Predbat deciding anything.
    """
    reset_inverter(my_predbat)
    failed = False

    saved = {key: getattr(my_predbat, key) for key in ["grid_power", "battery_power", "car_charging_power", "car_charging_power_configured"]}
    try:
        print("Test: with no car charging the surplus is the grid export")
        failed |= check_surplus(my_predbat, "plain export", grid_power=2000, battery_power=0, car_charging_power=0, car_configured=False, expect_state=2.0)

        print("Test: importing from the grid reports no surplus rather than a negative one")
        failed |= check_surplus(my_predbat, "importing", grid_power=-1500, battery_power=0, car_charging_power=0, car_configured=False, expect_state=0.0)

        print("Test: a charging car is added back, so the sensor does not collapse once the car starts")
        failed |= check_surplus(my_predbat, "car charging", grid_power=200, battery_power=0, car_charging_power=7000, car_configured=True, expect_state=7.2)

        # Cloud cover arriving mid-charge: PV has dropped, the battery is covering part of the car and
        # the grid has swung to import. Without subtracting the battery discharge this would still read
        # 4.0kW and an automation would happily keep the car charging out of the battery.
        print("Test: battery discharge is not offered to the car as solar surplus")
        failed |= check_surplus(my_predbat, "battery covering the car", grid_power=-3000, battery_power=3000, car_charging_power=7000, car_configured=True, expect_state=1.0)

        print("Test: battery charging is left in the surplus, as who gets it is the user's choice")
        failed |= check_surplus(my_predbat, "battery charging", grid_power=1000, battery_power=-2000, car_charging_power=0, car_configured=False, expect_state=1.0)

        print("Test: the components and whether a charger is configured are published as attributes")
        my_predbat.grid_power = 500
        my_predbat.battery_power = -250
        my_predbat.car_charging_power = 1500
        my_predbat.car_charging_power_configured = True
        my_predbat.publish_inverter_data()
        item = my_predbat.ha_interface.dummy_items.get(my_predbat.prefix + SURPLUS_ENTITY)
        expect = {
            "state": 2.0,
            "friendly_name": "Current solar surplus power",
            "state_class": "measurement",
            "unit_of_measurement": "kW",
            "device_class": "power",
            "icon": "mdi:solar-power",
            "grid_power": 0.5,
            "battery_power": -0.25,
            "car_charging_power": 1.5,
            "car_charging_power_configured": True,
        }
        for key, value in expect.items():
            if item.get(key, None) != value:
                print("ERROR: {} {} is {} expected {}".format(my_predbat.prefix + SURPLUS_ENTITY, key, item.get(key, None), value))
                failed = True

        print("Test: the sensor is published even when no car charger is configured")
        my_predbat.car_charging_power = 0
        my_predbat.car_charging_power_configured = False
        my_predbat.ha_interface.dummy_items.pop(my_predbat.prefix + SURPLUS_ENTITY, None)
        my_predbat.publish_inverter_data()
        item = my_predbat.ha_interface.dummy_items.get(my_predbat.prefix + SURPLUS_ENTITY)
        if item is None:
            print("ERROR: {} was not published with no car charger configured".format(my_predbat.prefix + SURPLUS_ENTITY))
            failed = True
        elif item.get("car_charging_power_configured", None) is not False:
            print("ERROR: {} car_charging_power_configured is {} expected False".format(my_predbat.prefix + SURPLUS_ENTITY, item.get("car_charging_power_configured", None)))
            failed = True
    finally:
        for key, value in saved.items():
            setattr(my_predbat, key, value)

    failed |= run_export_slot_tests(my_predbat)

    return failed
