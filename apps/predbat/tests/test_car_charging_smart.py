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

    return failed
