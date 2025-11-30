# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


def run_test_units(my_predbat):
    """
    Run the unit tests
    """
    print("Test units")
    failed = False
    ha = my_predbat.ha_interface

    ha.dummy_items["fred"] = {
        "state": 2,
        "unit_of_measurement": "kWh",
    }
    ha.dummy_items["joe"] = {
        "state": 2000,
        "unit_of_measurement": "W",
    }
    print("Test units 1")
    value = my_predbat.get_state_wrapper("fred")
    if float(value) != 2:
        print("ERROR: Expecting fred to be 2 got {}".format(value))
        failed = True
    print("Test units 2")
    value = my_predbat.get_state_wrapper("fred", required_unit="kWh")
    if float(value) != 2:
        print("ERROR: Expecting fred to be 2 got {}".format(value))
        failed = True
    print("Test units 3")
    value = my_predbat.get_state_wrapper("fred", required_unit="Wh")
    if float(value) != 2000:
        print("ERROR: Expecting fred to be 2000 got {}".format(value))
        failed = True
    print("Test units 4")
    value = my_predbat.get_state_wrapper("joe")
    if float(value) != 2000:
        print("ERROR: Expecting joe to be 2000 got {}".format(value))
        failed = True
    print("Test units 5")
    value = my_predbat.get_state_wrapper("joe", required_unit="W")
    if float(value) != 2000:
        print("ERROR: Expecting joe to be 2000 got {}".format(value))
        failed = True
    print("Test units 6")
    value = my_predbat.get_state_wrapper("joe", required_unit="kW")
    if float(value) != 2:
        print("ERROR: Expecting joe to be 2 got {}".format(value))
        failed = True
    print("Test units 7")
    my_predbat.set_state_wrapper("fred", 3, required_unit="kWh", attributes={"unit_of_measurement": "kWh"})
    value = my_predbat.get_state_wrapper("fred")
    if float(value) != 3:
        print("ERROR: Expecting fred to be 3 got {}".format(value))
        failed = True
    print("Test units 8")
    my_predbat.set_state_wrapper("fred", 4000, required_unit="Wh", attributes={"unit_of_measurement": "kWh"})
    value = my_predbat.get_state_wrapper("fred")
    if float(value) != 4:
        print("ERROR: Expecting fred to be 4 got {}".format(value))
        failed = True
    print("Test units 9")
    my_predbat.set_state_wrapper("joe", 3, required_unit="kW", attributes={"unit_of_measurement": "W"})
    value = my_predbat.get_state_wrapper("joe")
    if float(value) != 3000:
        print("ERROR: Expecting joe to be 3000 got {}".format(value))
        failed = True
    print("Test units 10")
    my_predbat.set_state_wrapper("joe", 4000, required_unit="W", attributes={"unit_of_measurement": "W"})
    value = my_predbat.get_state_wrapper("joe")
    if float(value) != 4000:
        print("ERROR: Expecting joe to be 4000 got {}".format(value))
        failed = True
    value = my_predbat.get_state_wrapper("joe", required_unit="kW")
    if float(value) != 4:
        print("ERROR: Expecting joe to be 4 got {}".format(value))
        failed = True

    print("Test units 11")
    ha.dummy_items["pete"] = {
        "state": 2000,
        "unit_of_measurement": "mA",
    }
    my_predbat.set_state_wrapper("pete", 5, required_unit="A", attributes={"unit_of_measurement": "mA"})
    value = my_predbat.get_state_wrapper("pete", required_unit="A")
    if float(value) != 5:
        print("ERROR: Expecting pete to be 5 got {}".format(value))
        failed = True
    value = my_predbat.get_state_wrapper("pete", required_unit="mA")
    if float(value) != 5000:
        print("ERROR: Expecting pete to be 5000 got {}".format(value))
        failed = True

    return failed
