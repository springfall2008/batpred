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

    # Test MW/MWh conversions (lines 249-254 in predbat.py)
    print("Test units 12: MW to kW conversion")
    ha.dummy_items["power_mw"] = {
        "state": 2.5,
        "unit_of_measurement": "MW",
    }
    value = my_predbat.get_state_wrapper("power_mw", required_unit="kW")
    if float(value) != 2500:
        print("ERROR: Expecting power_mw to be 2500 kW got {}".format(value))
        failed = True

    print("Test units 13: kW to MW conversion")
    ha.dummy_items["power_kw"] = {
        "state": 5000,
        "unit_of_measurement": "kW",
    }
    value = my_predbat.get_state_wrapper("power_kw", required_unit="MW")
    if float(value) != 5:
        print("ERROR: Expecting power_kw to be 5 MW got {}".format(value))
        failed = True

    print("Test units 14: MWh to kWh conversion")
    ha.dummy_items["energy_mwh"] = {
        "state": 1.25,
        "unit_of_measurement": "MWh",
    }
    value = my_predbat.get_state_wrapper("energy_mwh", required_unit="kWh")
    if float(value) != 1250:
        print("ERROR: Expecting energy_mwh to be 1250 kWh got {}".format(value))
        failed = True

    print("Test units 15: kWh to MWh conversion")
    ha.dummy_items["energy_kwh"] = {
        "state": 3500,
        "unit_of_measurement": "kWh",
    }
    value = my_predbat.get_state_wrapper("energy_kwh", required_unit="MWh")
    if float(value) != 3.5:
        print("ERROR: Expecting energy_kwh to be 3.5 MWh got {}".format(value))
        failed = True

    print("Test units 16: MW round-trip conversion")
    # Set a value in MW, then read it back in different units
    ha.dummy_items["power_test"] = {
        "state": 2,
        "unit_of_measurement": "MW",
    }
    value = my_predbat.get_state_wrapper("power_test", required_unit="kW")
    if float(value) != 2000:
        print("ERROR: Expecting power_test to be 2000 kW got {}".format(value))
        failed = True
    value = my_predbat.get_state_wrapper("power_test", required_unit="MW")
    if float(value) != 2:
        print("ERROR: Expecting power_test to be 2 MW got {}".format(value))
        failed = True

    print("Test units 17: kW to MW round-trip")
    ha.dummy_items["power_test2"] = {
        "state": 7500,
        "unit_of_measurement": "kW",
    }
    value = my_predbat.get_state_wrapper("power_test2", required_unit="MW")
    if float(value) != 7.5:
        print("ERROR: Expecting power_test2 to be 7.5 MW got {}".format(value))
        failed = True
    value = my_predbat.get_state_wrapper("power_test2", required_unit="kW")
    if float(value) != 7500:
        print("ERROR: Expecting power_test2 to be 7500 kW got {}".format(value))
        failed = True

    return failed
