# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from utils import calc_percent_limit
from tests.test_infra import DummyInverter


def run_inverter_multi_test(
    name,
    my_predbat,
    inverter,
    soc,
    isCharging=False,
    isExporting=False,
    battery_rate_max_charge=1.0,
    battery_rate_max_charge_all=1.0,
    soc_max=100.0,
    soc_max_all=100.0,
    soc_kw=0.0,
    soc_kw_all=0.0,
    assert_soc=0,
):
    print("Run scenario {}".format(name))
    failed = False
    inverter.battery_rate_max_charge = battery_rate_max_charge
    inverter.battery_rate_max_charge_dc = battery_rate_max_charge
    inverter.battery_rate_max_discharge = battery_rate_max_charge
    inverter.soc_max = soc_max
    inverter.soc_kw = soc_kw
    inverter.soc_percent = calc_percent_limit(soc_kw, soc_max)
    my_predbat.soc_max = soc_max_all
    my_predbat.soc_kw = soc_kw_all
    my_predbat.battery_rate_max_charge = battery_rate_max_charge_all
    my_predbat.battery_rate_max_charge_dc = battery_rate_max_charge_all

    # The target split shares the fleet's change between the inverters in my_predbat.inverters, so they must add up to the
    # fleet totals set above. The inverter under test is one of them and a second inverter stands in for all the others
    my_predbat.inverters = [inverter]
    if soc_max_all > soc_max or battery_rate_max_charge_all > battery_rate_max_charge:
        others = DummyInverter(my_predbat.log, inverter_id=1)
        others.soc_kw = soc_kw_all - soc_kw
        others.soc_max = soc_max_all - soc_max
        others.battery_rate_max_charge = battery_rate_max_charge_all - battery_rate_max_charge
        others.battery_rate_max_charge_dc = others.battery_rate_max_charge
        others.battery_rate_max_discharge = others.battery_rate_max_charge
        my_predbat.inverters.append(others)

    my_predbat.adjust_battery_target_multi(inverter, soc, isCharging, isExporting)
    if assert_soc != inverter.soc_target:
        print("ERROR: SOC {} should be {}".format(inverter.soc_target, assert_soc))
        failed = True
    if isCharging != inverter.isCharging:
        print("ERROR: isCharging {} should be {}".format(inverter.isCharging, isCharging))
        failed = True
    return failed


def run_inverter_multi_tests(my_predbat):
    print("**** Running inverter multi tests ****\n")

    failed = False
    inverter = DummyInverter(my_predbat.log)
    saved_inverters = my_predbat.inverters
    saved_totals = (my_predbat.soc_max, my_predbat.soc_kw, my_predbat.battery_rate_max_charge, my_predbat.battery_rate_max_charge_dc)

    failed |= run_inverter_multi_test("charge", my_predbat, inverter, 50, isCharging=False, assert_soc=50)
    failed |= run_inverter_multi_test("charge_soc", my_predbat, inverter, 50, isCharging=False, assert_soc=50, soc_kw=50.0, soc_kw_all=50.0)
    failed |= run_inverter_multi_test("charge2", my_predbat, inverter, 50, isCharging=False, assert_soc=50, battery_rate_max_charge_all=2.0, soc_max_all=200.0)
    failed |= run_inverter_multi_test("charge3", my_predbat, inverter, 50, isCharging=False, assert_soc=75, battery_rate_max_charge_all=2.0, soc_max=50.0, soc_max_all=150.0)
    failed |= run_inverter_multi_test("charge4", my_predbat, inverter, 50, isCharging=True, assert_soc=67, battery_rate_max_charge=2.0, battery_rate_max_charge_all=3.0, soc_max_all=200.0)
    failed |= run_inverter_multi_test("charge5", my_predbat, inverter, 50, isCharging=True, assert_soc=33, battery_rate_max_charge=1.0, battery_rate_max_charge_all=3.0, soc_max_all=200.0)
    failed |= run_inverter_multi_test("discharge", my_predbat, inverter, 50, isCharging=False, assert_soc=50, soc_max_all=200.0, soc_max=100.0, soc_kw=100.0, soc_kw_all=200.0, battery_rate_max_charge_all=2.0)
    # The other inverter is already full, so this one takes all of the 50kWh to add: 0kWh -> 50kWh, where it used to be given only its half (25%) and the rest was lost (#4856 F2)
    failed |= run_inverter_multi_test("charge_other_full", my_predbat, inverter, 75, isCharging=True, assert_soc=50, soc_max_all=200.0, soc_max=100.0, soc_kw=0.0, soc_kw_all=100.0, battery_rate_max_charge_all=2.0)

    # An inverter that is not one of the fleet cannot have the fleet target shared out to it, which must say so clearly
    print("Run scenario inverter_not_in_fleet")
    my_predbat.soc_max = 100.0
    my_predbat.soc_kw = 0.0
    my_predbat.inverters = [DummyInverter(my_predbat.log, inverter_id=7)]
    try:
        my_predbat.adjust_battery_target_multi(inverter, 50, False, False)
        print("ERROR: an inverter that is not in the fleet should have raised an error")
        failed = True
    except ValueError as error:
        if "is not one of the 1 inverters in self.inverters" not in str(error):
            print("ERROR: the error for an inverter that is not in the fleet should say so, got: {}".format(error))
            failed = True

    my_predbat.inverters = saved_inverters
    my_predbat.soc_max, my_predbat.soc_kw, my_predbat.battery_rate_max_charge, my_predbat.battery_rate_max_charge_dc = saved_totals
    return failed
