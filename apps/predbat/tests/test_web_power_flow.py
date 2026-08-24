# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from config import APPS_SCHEMA
from web import WebInterface


def make_web(my_predbat):
    """Create a WebInterface instance bound to the given predbat."""
    return WebInterface(my_predbat, web_port=5053)


def set_power_sensor(my_predbat, entity_id, watts):
    """Publish a power sensor into the HA mock for get_arg() to resolve."""
    my_predbat.ha_interface.dummy_items[entity_id] = {"state": watts, "unit_of_measurement": "W"}


def run_web_power_flow_tests(my_predbat):
    """Car charging power input, published sensor and its arm of the power flow diagram."""
    failed = 0
    print("**** Running web power flow car charging tests ****")

    original_args = my_predbat.args.copy()
    original_load_power = my_predbat.load_power
    web = make_web(my_predbat)

    # -------------------------------------------------------------------------
    print("Test: car_charging_power is a known apps.yaml key")
    if "car_charging_power" not in APPS_SCHEMA:
        print("  ERROR: car_charging_power missing from APPS_SCHEMA, apps.yaml validation would reject it")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: no car_charging_power configured leaves the reading at zero and unconfigured")
    my_predbat.args.pop("car_charging_power", None)
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power != 0:
        print(f"  ERROR: expected 0 W with nothing configured, got {my_predbat.car_charging_power}")
        failed += 1
    if my_predbat.car_charging_power_configured:
        print("  ERROR: car_charging_power_configured should be False when the key is not set")
        failed += 1

    # -------------------------------------------------------------------------
    # The apps.yaml templates ship car_charging_power as a regular expression matching the
    # common chargers. auto_config(final=True) deletes it when nothing matches, but until then
    # the literal "re:" string is still sitting in args - and a household with no car charger
    # must not get a Car node drawn from it
    print("Test: an unmatched regular expression default counts as not configured")
    my_predbat.args["car_charging_power"] = "re:(sensor.myenergi_zappi_[0-9a-z]+_internal_load_ct1|sensor.wallbox_portal_charging_power)"
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power_configured:
        print("  ERROR: an unresolved 're:' expression should not count as a configured charger")
        failed += 1
    if my_predbat.car_charging_power != 0:
        print(f"  ERROR: expected 0 W from an unresolved 're:' expression, got {my_predbat.car_charging_power}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a single car_charging_power sensor is read")
    set_power_sensor(my_predbat, "sensor.car_charger_power", 3200)
    my_predbat.args["car_charging_power"] = "sensor.car_charger_power"
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power != 3200:
        print(f"  ERROR: expected 3200 W from the configured sensor, got {my_predbat.car_charging_power}")
        failed += 1
    if not my_predbat.car_charging_power_configured:
        print("  ERROR: car_charging_power_configured should be True once the key is set")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: multiple chargers are summed")
    set_power_sensor(my_predbat, "sensor.car_charger_power_2", 1500)
    my_predbat.args["car_charging_power"] = ["sensor.car_charger_power", "sensor.car_charger_power_2"]
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power != 4700:
        print(f"  ERROR: expected 3200 + 1500 = 4700 W summed over both chargers, got {my_predbat.car_charging_power}")
        failed += 1

    # -------------------------------------------------------------------------
    # auto_config() replaces a list entry whose regular expression found nothing with None,
    # leaving holes in the middle of the list rather than shortening it
    print("Test: a hole in the sensor list does not hide the chargers after it")
    my_predbat.args["car_charging_power"] = [None, "sensor.car_charger_power_2"]
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power != 1500:
        print(f"  ERROR: expected the second charger's 1500 W to still be read past the hole, got {my_predbat.car_charging_power}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: kW sensors are converted to W")
    my_predbat.ha_interface.dummy_items["sensor.car_charger_power_kw"] = {"state": 7.2, "unit_of_measurement": "kW"}
    my_predbat.args["car_charging_power"] = "sensor.car_charger_power_kw"
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power != 7200:
        print(f"  ERROR: expected 7.2 kW to be read as 7200 W, got {my_predbat.car_charging_power}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: an unavailable sensor reads as zero rather than breaking the total")
    my_predbat.ha_interface.dummy_items["sensor.car_charger_offline"] = {"state": "unavailable", "unit_of_measurement": "W"}
    my_predbat.args["car_charging_power"] = ["sensor.car_charger_power", "sensor.car_charger_offline"]
    original_had_errors = my_predbat.had_errors
    my_predbat.had_errors = False
    my_predbat.update_car_charging_power()
    if my_predbat.car_charging_power != 3200:
        print(f"  ERROR: expected the good charger's 3200 W with the other unavailable, got {my_predbat.car_charging_power}")
        failed += 1
    if not my_predbat.car_charging_power_configured:
        print("  ERROR: an unavailable sensor should still count as configured")
        failed += 1
    # A charger reporting 'unavailable' while nothing is plugged in is normal, so it must not
    # flag the run as errored - that would leave Predbat sitting in "with Errors" all day
    if my_predbat.had_errors:
        print("  ERROR: an unavailable car charger sensor should not put the run into an error state")
        failed += 1
    my_predbat.had_errors = original_had_errors

    # -------------------------------------------------------------------------
    print("Test: car charging power is published as a sensor for upstream consumers")
    my_predbat.args["car_charging_power"] = "sensor.car_charger_power"
    my_predbat.update_car_charging_power()
    my_predbat.publish_inverter_data()
    entity = my_predbat.prefix + ".car_charging_power"
    attrs = my_predbat.ha_interface.dummy_items.get(entity)
    if not attrs:
        print(f"  ERROR: {entity} was not published")
        failed += 1
    else:
        if attrs.get("state") != 3.2:
            print(f"  ERROR: expected {entity} to publish 3.2 kW, got {attrs.get('state')}")
            failed += 1
        if attrs.get("unit_of_measurement") != "kW":
            print(f"  ERROR: expected {entity} in kW like the other power sensors, got {attrs.get('unit_of_measurement')}")
            failed += 1
        if attrs.get("device_class") != "power":
            print(f"  ERROR: expected {entity} to carry device_class power, got {attrs.get('device_class')}")
            failed += 1

    # -------------------------------------------------------------------------
    print("Test: the sensor is not published when no charger is configured")
    my_predbat.ha_interface.dummy_items.pop(entity, None)
    my_predbat.args.pop("car_charging_power", None)
    my_predbat.update_car_charging_power()
    my_predbat.publish_inverter_data()
    if entity in my_predbat.ha_interface.dummy_items:
        print(f"  ERROR: {entity} should not be published when no car charging power sensor is configured")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: the flow diagram is unchanged when no car charging power is configured")
    my_predbat.load_power = 3000
    my_predbat.car_charging_power = 0
    my_predbat.car_charging_power_configured = False
    html = web.get_power_flow_diagram()
    if ">Car<" in html:
        print("  ERROR: the Car node should not be drawn when no car charging power sensor is configured")
        failed += 1
    if ">3000 W<" not in html:
        print("  ERROR: the House circle should show the full load power when there is no car to subtract")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a charging car is drawn with its own arm and subtracted from the house load")
    my_predbat.load_power = 3000
    my_predbat.car_charging_power = 2000
    my_predbat.car_charging_power_configured = True
    html = web.get_power_flow_diagram()
    if ">Car<" not in html:
        print("  ERROR: expected a Car node in the diagram once a car charging power sensor is configured")
        failed += 1
    if ">2000 W<" not in html:
        print("  ERROR: expected the car charging power to be labelled on its arrow")
        failed += 1
    if ">1000 W<" not in html:
        print("  ERROR: expected the House circle to show the load remainder (3000 - 2000) once the car is drawn separately")
        failed += 1
    if "car-house-path" in html and "house-car-path" not in html:
        print("  ERROR: the car arm should flow from the house to the car, not the other way")
        failed += 1
    if "animateMotion" not in html:
        print("  ERROR: expected animated flow dots on the car arm while it is charging")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a configured but idle charger keeps its node with a dashed arm")
    my_predbat.car_charging_power = 0
    my_predbat.car_charging_power_configured = True
    html = web.get_power_flow_diagram()
    if ">Car<" not in html:
        print("  ERROR: a configured charger should stay on the diagram when it is not charging")
        failed += 1
    car_arm = html[html.find("<!-- House to Car") :] if "<!-- House to Car" in html else ""
    if "stroke-dasharray" not in car_arm:
        print("  ERROR: expected the idle car arm to be dashed, as the PV arm is when not generating")
        failed += 1
    if ">3000 W<" not in html:
        print("  ERROR: the House circle should show the full load power when the car is drawing nothing")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a car reading above the house load clamps the house remainder at zero")
    my_predbat.load_power = 1000
    my_predbat.car_charging_power = 3000
    my_predbat.car_charging_power_configured = True
    html = web.get_power_flow_diagram()
    if ">0 W<" not in html:
        print("  ERROR: expected the House remainder to clamp at 0 W rather than go negative")
        failed += 1
    if "-2000 W" in html:
        print("  ERROR: the House circle must never show a negative load")
        failed += 1

    my_predbat.args = original_args
    my_predbat.load_power = original_load_power
    my_predbat.car_charging_power = 0
    my_predbat.car_charging_power_configured = False

    print("**** Web power flow car charging tests completed ****")
    return failed
