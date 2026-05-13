# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from tests.test_infra import reset_rates2, reset_inverter


def run_car_charging_smart_test(test_name, my_predbat, battery_size=10.0, limit=8.0, soc=0, rate=10.0, loss=1.0, max_price=99, smart=True, plan_time="00:00:00", expect_cost=0, expect_kwh=0):
    """
    Run a car charging smart test
    """
    failed = False

    print("**** Running Test: {} ****".format(test_name))

    my_predbat.car_charging_battery_size = [battery_size]
    my_predbat.car_charging_limit = [limit]
    my_predbat.car_charging_soc = [soc]
    my_predbat.car_charging_soc_next = [None]
    my_predbat.car_charging_rate = [rate]
    my_predbat.car_charging_loss = loss
    my_predbat.car_charging_plan_max_price = [max_price]
    my_predbat.car_charging_plan_smart = [smart]
    my_predbat.car_charging_plan_time = [plan_time]
    my_predbat.num_cars = 1

    my_predbat.car_charging_slots[0] = my_predbat.plan_car_charging(0, my_predbat.low_rates)
    total_kwh = 0
    total_cost = 0
    for slot in my_predbat.car_charging_slots[0]:
        total_kwh += slot["kwh"]
        total_cost += slot["cost"]
    if total_kwh != expect_kwh:
        print("ERROR: Car charging total kwh should be {} got {}".format(expect_kwh, total_kwh))
        failed = True
    total_pd = my_predbat.car_charge_slot_kwh(my_predbat.minutes_now, my_predbat.minutes_now + my_predbat.forecast_minutes)
    if total_pd != expect_kwh:
        print("ERROR: Car charging total calculated with car_charge_slot_kwh should be {} got {}".format(expect_kwh, total_pd))
        failed = True
    if total_cost != expect_cost:
        print("ERROR: Car charging total cost should be {} got {}".format(expect_cost, total_cost))
        failed = True

    return failed


def run_car_charging_slot_integer_test(test_name, my_predbat, battery_size, soc, limit, rate, loss=1.0, smart=True, plan_time="07:00:00"):
    """
    Verify that plan_car_charging always produces slots with integer start/end values.

    Regression test for issue #3911: when length-clamping was triggered (kwh_add > kwh_left),
    round(x, 0) returned a Python float, causing slot['end'] to be a float (e.g. 1570.0).
    This would later crash range(start, float_end, step) in yesterday_reconstruct_car_slots.
    """
    failed = False
    print("**** Running Test: {} ****".format(test_name))

    # Save state that will be modified
    saved_battery_size = my_predbat.car_charging_battery_size
    saved_limit = my_predbat.car_charging_limit
    saved_soc = my_predbat.car_charging_soc
    saved_soc_next = my_predbat.car_charging_soc_next
    saved_rate = my_predbat.car_charging_rate
    saved_loss = my_predbat.car_charging_loss
    saved_max_price = my_predbat.car_charging_plan_max_price
    saved_smart = my_predbat.car_charging_plan_smart
    saved_plan_time = my_predbat.car_charging_plan_time
    saved_num_cars = my_predbat.num_cars

    my_predbat.car_charging_battery_size = [battery_size]
    my_predbat.car_charging_limit = [limit]
    my_predbat.car_charging_soc = [soc]
    my_predbat.car_charging_soc_next = [None]
    my_predbat.car_charging_rate = [rate]
    my_predbat.car_charging_loss = loss
    my_predbat.car_charging_plan_max_price = [99]
    my_predbat.car_charging_plan_smart = [smart]
    my_predbat.car_charging_plan_time = [plan_time]
    my_predbat.num_cars = 1

    slots = my_predbat.plan_car_charging(0, my_predbat.low_rates)
    for slot in slots:
        if not isinstance(slot["start"], int):
            print("ERROR: slot start is not int: {} (type {})".format(slot["start"], type(slot["start"])))
            failed = True
        if not isinstance(slot["end"], int):
            print("ERROR: slot end is not int: {} (type {})".format(slot["end"], type(slot["end"])))
            failed = True

    # Restore state
    my_predbat.car_charging_battery_size = saved_battery_size
    my_predbat.car_charging_limit = saved_limit
    my_predbat.car_charging_soc = saved_soc
    my_predbat.car_charging_soc_next = saved_soc_next
    my_predbat.car_charging_rate = saved_rate
    my_predbat.car_charging_loss = saved_loss
    my_predbat.car_charging_plan_max_price = saved_max_price
    my_predbat.car_charging_plan_smart = saved_smart
    my_predbat.car_charging_plan_time = saved_plan_time
    my_predbat.num_cars = saved_num_cars

    return failed


def run_car_charging_smart_tests(my_predbat):
    """
    Test car charging smart
    """
    failed = False
    reset_inverter(my_predbat)

    print("**** Running Car Charging Smart tests ****")
    import_rate = 10.0
    export_rate = 5.0
    reset_rates2(my_predbat, import_rate, export_rate)
    my_predbat.low_rates, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False)

    failed |= run_car_charging_smart_test("smart1", my_predbat, battery_size=12.0, limit=10.0, soc=0, rate=10.0, loss=1.0, max_price=99, smart=True, expect_cost=100, expect_kwh=10)
    failed |= run_car_charging_smart_test("smart2", my_predbat, battery_size=12.0, limit=10.0, soc=0, rate=10.0, loss=1.0, max_price=99, smart=False, expect_cost=150, expect_kwh=10)
    failed |= run_car_charging_smart_test("smart3", my_predbat, battery_size=12.0, limit=10.0, soc=2, rate=10.0, loss=1.0, max_price=99, smart=True, expect_cost=80, expect_kwh=8)
    failed |= run_car_charging_smart_test("smart4", my_predbat, battery_size=12.0, limit=10.0, soc=2, rate=10.0, loss=0.5, max_price=99, smart=True, expect_cost=160, expect_kwh=16)
    failed |= run_car_charging_smart_test("smart5", my_predbat, battery_size=100.0, limit=100.0, soc=0, rate=1.0, loss=1, max_price=99, smart=True, expect_cost=12 * 15, expect_kwh=12, plan_time="00:00:00")
    failed |= run_car_charging_smart_test("smart6", my_predbat, battery_size=100.0, limit=100.0, soc=0, rate=1.0, loss=1, max_price=99, smart=True, expect_cost=14 * 15, expect_kwh=14, plan_time="02:00:00")
    failed |= run_car_charging_smart_test("smart7", my_predbat, battery_size=100.0, limit=100.0, soc=0, rate=1.0, loss=1, max_price=10, smart=True, expect_cost=7 * 10, expect_kwh=7, plan_time="02:00:00")
    failed |= run_car_charging_smart_test("smart8", my_predbat, battery_size=100.0, limit=100.0, soc=0, rate=1.0, loss=1, max_price=10, smart=False, expect_cost=7 * 10, expect_kwh=7, plan_time="02:00:00")

    # Regression test for issue #3911: fractional battery size triggers length-clamping
    # which used round(x, 0) (returns float in Python 3) causing float slot ends
    # that crash range() in yesterday_reconstruct_car_slots.
    failed |= run_car_charging_slot_integer_test("smart_int1_issue3911", my_predbat, battery_size=11.9, soc=9.9, limit=11.9, rate=7.4, loss=1.0, plan_time="07:00:00")
    failed |= run_car_charging_slot_integer_test("smart_int2_issue3911", my_predbat, battery_size=11.9, soc=0.0, limit=11.9, rate=7.4, loss=1.0, plan_time="07:00:00")
    failed |= run_car_charging_slot_integer_test("smart_int3_issue3911", my_predbat, battery_size=77.0, soc=74.3, limit=77.0, rate=11.0, loss=0.9, plan_time="07:00:00")

    return failed
