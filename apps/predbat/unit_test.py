# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import copy
import os
import re
import time
import math
import sys
from datetime import datetime, timedelta
import hashlib
import traceback

import pytz
import requests
import yaml
from multiprocessing import Pool, cpu_count, set_start_method
import asyncio
import json
import matplotlib
import matplotlib.pyplot as plt
import numpy as np

from predbat import PredBat
from prediction import Prediction
from prediction import wrapped_run_prediction_single

KEEP_SCALE = 0.5


class TestHAInterface:
    def __init__(self):
        self.step = 5
        self.build_history()
        pass

    def build_history(self, days=30):
        history = []
        now = datetime.now()
        start = now - timedelta(days=days)
        state = 0.0
        for count in range(int(days * 24 * 60 / self.step)):
            point = start + timedelta(minutes=count * self.step)
            point_str = point.strftime("%Y-%m-%dT%H:%M:%SZ")
            history.append({"state": state, "last_changed": point})
        self.history = history

    def get_state(self, entity_id, default=None, attribute=None, refresh=False):
        print("Getting state: {}".format(entity_id))
        if not entity_id:
            return {}
        else:
            return None

    def call_service(self, domain, service, data):
        print("Calling service: {} {}".format(domain, service))
        return None

    def set_state(self, entity_id, state, attributes=None):
        print("Setting state: {} to {}".format(entity_id, state))
        return None

    def get_history(self, entity_id, now=None, days=30):
        print("Getting history for {}".format(entity_id))
        return [self.history]


class TestInverter:
    def __init__(self):
        pass


def reset_rates(my_predbat, ir, xr):
    my_predbat.combine_charge_slots = True
    for minute in range(my_predbat.forecast_minutes + my_predbat.minutes_now):
        my_predbat.rate_import[minute] = ir
        my_predbat.rate_export[minute] = xr
    my_predbat.rate_export_min = xr

    low_rates, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, 9999, False)
    return low_rates


def reset_rates2(my_predbat, ir, xr):
    my_predbat.combine_charge_slots = True
    for minute in range(my_predbat.forecast_minutes + my_predbat.minutes_now):
        if minute % 60 < 30:
            my_predbat.rate_import[minute] = ir
            my_predbat.rate_export[minute] = xr
        else:
            my_predbat.rate_import[minute] = ir * 2
            my_predbat.rate_export[minute] = xr * 2
    my_predbat.rate_export_min = xr

    low_rates, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, 9999, False)
    return low_rates


def update_rates_import(my_predbat, charge_window_best):
    for window in charge_window_best:
        for minute in range(window["start"], window["end"]):
            my_predbat.rate_import[minute] = window["average"]


def update_rates_export(my_predbat, discharge_window_best):
    for window in discharge_window_best:
        for minute in range(window["start"], window["end"]):
            my_predbat.rate_export[minute] = window["average"]


def reset_inverter(my_predbat):
    my_predbat.inverter_limit = 1 / 60.0
    my_predbat.num_inverters = 1
    my_predbat.export_limit = 10 / 60.0
    my_predbat.inverters = [TestInverter()]
    my_predbat.charge_window = []
    my_predbat.discharge_window = []
    my_predbat.discharge_limits = []
    my_predbat.current_charge_limit = 0
    my_predbat.soc_kw = 0.0
    my_predbat.soc_max = 100.0
    my_predbat.reserve = 0.0
    my_predbat.reserve_percent = 0.0
    my_predbat.reserve_current = 0.0
    my_predbat.reserve_current_percent = 0.0
    my_predbat.battery_rate_max_charge = 1 / 60.0
    my_predbat.battery_rate_max_discharge = 1 / 60.0
    my_predbat.battery_rate_max_charge_scaled = 1 / 60.0
    my_predbat.battery_rate_max_discharge_scaled = 1 / 60.0
    my_predbat.battery_rate_min = 0
    my_predbat.charge_rate_now = 1 / 60.0
    my_predbat.discharge_rate_now = 1 / 60.0
    my_predbat.pv_power = 0
    my_predbat.load_power = 0
    my_predbat.battery_loss = 1.0
    my_predbat.inverter_loss = 1.0
    my_predbat.battery_loss_discharge = 1.0
    my_predbat.inverter_hybrid = False
    my_predbat.battery_charge_power_curve = {}
    my_predbat.battery_discharge_power_curve = {}
    my_predbat.battery_rate_max_scaling = 1.0
    my_predbat.battery_rate_max_scaling_discharge = 1.0
    my_predbat.metric_battery_cycle = 0
    my_predbat.num_cars = 0
    my_predbat.car_charging_slots[0] = []
    my_predbat.car_charging_from_battery = True
    my_predbat.car_charging_soc[0] = 0
    my_predbat.car_charging_limit[0] = 100.0
    my_predbat.iboost_enable = False
    my_predbat.iboost_solar = False
    my_predbat.iboost_gas = False
    my_predbat.iboost_charging = False
    my_predbat.iboost_rate = False
    my_predbat.iboost_smart = False
    my_predbat.minutes_now = 12 * 60
    my_predbat.best_soc_keep = 0.0
    my_predbat.carbon_enable = 0


def plot(name, prediction):
    """
    Plot the prediction
    """
    fig, ax = plt.subplots()
    # Predict_soc is a hash on minutes since the start of simulation and the SOC value
    # Convert this into a NP array for plotting
    minutes = np.array(list(prediction.predict_soc.keys()))
    predict_soc = np.array(list(prediction.predict_soc.values()))
    metric_pence = list(prediction.predict_metric_best.values())
    metric = [round(x / 100, 2) for x in metric_pence]
    metric = np.array(metric)
    ax.plot(minutes, predict_soc, label="soc")
    ax.plot(minutes, metric, label="metric")
    ax.set_xticks(range(0, prediction.forecast_minutes, 240))
    ax.set(xlabel="time (minutes)", ylabel="Value", title=name)
    ax.legend()
    plt.savefig("{}.png".format(name))
    plt.show()


# def compute_metric(self, end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh):


def compute_metric_test(
    my_predbat,
    name,
    end_record=None,
    soc=0,
    soc10=0,
    cost=0,
    cost10=0,
    final_iboost=0,
    final_iboost10=0,
    battery_cycle=0,
    metric_keep=0,
    final_carbon_g=0,
    import_kwh_battery=0,
    import_kwh_house=0,
    export_kwh=0,
    assert_metric=0,
    battery_value_scaling=1.0,
    rate_export_min=1.0,
    iboost_value_scaling=1.0,
    inverter_loss=1.0,
    battery_loss=1.0,
    metric_battery_cycle=0.0,
    pv_metric10_weight=0.0,
    battery_loss_discharge=1.0,
    metric_self_sufficiency=0.0,
    carbon_metric=0.0,
    rate_min=1.0,
):
    """
    Test the compute metric function
    """
    my_predbat.metric_battery_value_scaling = battery_value_scaling
    my_predbat.rate_export_min = rate_export_min
    my_predbat.iboost_value_scaling = iboost_value_scaling
    my_predbat.inverter_loss = inverter_loss
    my_predbat.battery_loss = battery_loss
    my_predbat.metric_battery_cycle = metric_battery_cycle
    my_predbat.pv_metric10_weight = pv_metric10_weight
    my_predbat.battery_loss_discharge = battery_loss_discharge
    my_predbat.metric_self_sufficiency = metric_self_sufficiency
    my_predbat.rate_min = rate_min
    if not end_record:
        end_record = my_predbat.forecast_minutes

    my_predbat.rate_min_forward = {n: rate_min for n in range(my_predbat.forecast_minutes + my_predbat.minutes_now)}
    if carbon_metric:
        my_predbat.carbon_enable = True
        my_predbat.carbon_metric = carbon_metric
    else:
        my_predbat.carbon_enable = False
        my_predbat.carbon_metric = 99

    print("Metric Test {}".format(name))

    metric = my_predbat.compute_metric(
        end_record,
        soc,
        soc10,
        cost,
        cost10,
        final_iboost,
        final_iboost10,
        battery_cycle,
        metric_keep,
        final_carbon_g,
        import_kwh_battery,
        import_kwh_house,
        export_kwh,
    )
    if abs(metric - assert_metric) > 0.1:
        print("ERROR: Test {} Metric {} should be {}".format(name, metric, assert_metric))
        return True
    return False


def run_compute_metric_tests(my_predbat):
    """
    Test the compute metric function
    """
    failed = False
    failed |= compute_metric_test(my_predbat, "zero", assert_metric=0)
    failed |= compute_metric_test(my_predbat, "cost", cost=10.0, assert_metric=10)
    failed |= compute_metric_test(my_predbat, "cost_bat", cost=10.0, soc=10, rate_min=5, assert_metric=10 - 5 * 10)
    failed |= compute_metric_test(my_predbat, "cost_iboost", cost=10.0, final_iboost=50, iboost_value_scaling=0.8, assert_metric=10 - 50 * 0.8)
    failed |= compute_metric_test(my_predbat, "cost_keep", cost=10.0, metric_keep=5, assert_metric=10 + 5)
    failed |= compute_metric_test(my_predbat, "cost10", cost=10.0, cost10=20, pv_metric10_weight=0.5, assert_metric=10 + 10 * 0.5)
    failed |= compute_metric_test(my_predbat, "cost_carbon", cost=10.0, final_carbon_g=100, carbon_metric=2.0, assert_metric=10 + 100 / 1000 * 2.0)
    failed |= compute_metric_test(my_predbat, "cost_battery_cycle", cost=10.0, battery_cycle=25, metric_battery_cycle=0.1, assert_metric=10 + 25 * 0.1)
    return failed


def simple_scenario(
    name,
    my_predbat,
    load_amount,
    pv_amount,
    assert_final_metric,
    assert_final_soc,
    with_battery=True,
    battery_loss=1.0,
    battery_size=100.0,
    battery_soc=0.0,
    hybrid=False,
    export_limit=10.0,
    inverter_limit=1.0,
    reserve=0.0,
    charge=0,
    discharge=100,
    charge_window_best=[],
    inverter_loss=1.0,
    battery_rate_max_charge=1.0,
    charge_car=0,
    car_charging_from_battery=True,
    iboost_solar=False,
    iboost_gas=False,
    rate_gas=0,
    gas_scale=1.0,
    iboost_charging=False,
    iboost_max_energy=100.0,
    assert_final_iboost=0.0,
    end_record=None,
    pv10=False,
    carbon=0,
    assert_final_carbon=0.0,
    keep=0.0,
    assert_keep=0.0,
    save="best",
    quiet=False,
    low_rates=[],
    iboost_rate=False,
    iboost_rate_threshold=9999,
    iboost_smart=False,
):
    """
    No PV, No Load
    """
    if not quiet:
        print("Run scenario {}".format(name))

    battery_rate = 1.0 if with_battery else 0.0
    my_predbat.battery_loss = battery_loss
    my_predbat.battery_loss_discharge = battery_loss
    my_predbat.battery_rate_max_scaling = battery_rate
    my_predbat.battery_rate_max_scaling_discharge = battery_rate
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.inverter_hybrid = hybrid
    my_predbat.export_limit = export_limit / 60.0
    my_predbat.inverter_limit = inverter_limit / 60.0
    my_predbat.reserve = reserve
    my_predbat.inverter_loss = inverter_loss
    my_predbat.battery_rate_max_charge = battery_rate_max_charge / 60.0
    my_predbat.battery_rate_max_discharge = battery_rate_max_charge / 60.0
    my_predbat.battery_rate_max_charge_scaled = battery_rate_max_charge / 60.0
    my_predbat.battery_rate_max_discharge_scaled = battery_rate_max_charge / 60.0
    my_predbat.car_charging_from_battery = car_charging_from_battery

    my_predbat.iboost_enable = iboost_solar or iboost_gas or iboost_charging or iboost_rate
    my_predbat.iboost_gas = iboost_gas
    my_predbat.iboost_solar = iboost_solar
    my_predbat.iboost_smart = iboost_smart
    my_predbat.iboost_rate = iboost_rate
    my_predbat.iboost_rate_threshold = iboost_rate_threshold
    my_predbat.iboost_min_power = 0.0
    my_predbat.iboost_max_power = export_limit / 60.0
    my_predbat.iboost_max_energy = iboost_max_energy
    my_predbat.rate_gas = {n: rate_gas for n in range(my_predbat.forecast_minutes + my_predbat.minutes_now)}
    my_predbat.iboost_gas_scale = gas_scale
    my_predbat.iboost_charging = iboost_charging
    my_predbat.best_soc_keep = keep
    my_predbat.car_charging_soc[0] = 0
    my_predbat.car_charging_limit[0] = 100.0

    if my_predbat.iboost_enable and (iboost_gas or iboost_rate or iboost_smart):
        my_predbat.iboost_plan = my_predbat.plan_iboost_smart(low_rates)
        # print("IBoost plan {} low_rates {} rate_gas {}".format(my_predbat.iboost_plan, low_rates, rate_gas))
    else:
        my_predbat.iboost_plan = []

    if end_record:
        my_predbat.end_record = end_record
    else:
        my_predbat.end_record = my_predbat.forecast_minutes

    my_predbat.carbon_intensity = {n: carbon for n in range(my_predbat.forecast_minutes + my_predbat.minutes_now)}
    my_predbat.carbon_enable = carbon

    assert_final_metric = round(assert_final_metric / 100.0, 2)
    assert_final_soc = round(assert_final_soc, 2)
    pv_step = {}
    load_step = {}
    pv10_step = {}
    load10_step = {}

    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = pv_amount / (60 / 5) if not pv10 else 0
        load_step[minute] = load_amount / (60 / 5) if not pv10 else 0

    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv10_step[minute] = pv_amount / (60 / 5) if pv10 else 0
        load10_step[minute] = load_amount / (60 / 5) if pv10 else 0

    if charge_car:
        my_predbat.num_cars = 1
        my_predbat.car_charging_slots[0] = [
            {"start": my_predbat.minutes_now, "end": my_predbat.forecast_minutes + my_predbat.minutes_now, "kwh": charge_car * my_predbat.forecast_minutes / 60.0}
        ]
    else:
        my_predbat.num_cars = 0
        my_predbat.car_charging_slots[0] = []

    prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step)

    charge_limit_best = []
    if charge > 0:
        charge_limit_best = [charge]
        if not charge_window_best:
            charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.forecast_minutes + my_predbat.minutes_now, "average": 0}]
    discharge_limit_best = []
    discharge_window_best = []
    if discharge < 100:
        discharge_limit_best = [discharge]
        discharge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.forecast_minutes + my_predbat.minutes_now, "average": 0}]
    if save == "none":
        (
            metric,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            final_soc,
            soc_min_minute,
            battery_cycle,
            metric_keep,
            final_iboost,
            final_carbon_g,
        ) = wrapped_run_prediction_single(charge_limit_best, charge_window_best, discharge_window_best, discharge_limit_best, pv10, end_record=(my_predbat.end_record), step=5)
    else:
        (
            metric,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            final_soc,
            soc_min_minute,
            battery_cycle,
            metric_keep,
            final_iboost,
            final_carbon_g,
        ) = prediction.run_prediction(charge_limit_best, charge_window_best, discharge_window_best, discharge_limit_best, pv10, end_record=(my_predbat.end_record), save=save)
    metric = round(metric / 100.0, 2)
    final_soc = round(final_soc, 2)
    final_iboost = round(final_iboost, 2)

    failed = False
    if abs(metric - assert_final_metric) >= 0.1:
        print("ERROR: Metric {} should be {}".format(metric, assert_final_metric))
        failed = True
    if abs(final_soc - assert_final_soc) >= 0.1:
        print("ERROR: Final SOC {} should be {}".format(final_soc, assert_final_soc))
        failed = True
    if abs(final_iboost - assert_final_iboost) >= 0.1:
        print("ERROR: Final iBoost {} should be {}".format(final_iboost, assert_final_iboost))
        failed = True
    if abs(final_carbon_g - assert_final_carbon) >= 0.1:
        print("ERROR: Final Carbon {} should be {}".format(final_carbon_g, assert_final_carbon))
        failed = True
    if abs(metric_keep - assert_keep) >= 0.1:
        print("ERROR: Metric keep {} should be {}".format(metric_keep, assert_keep))
        failed = True

    if failed:
        prediction.run_prediction(charge_limit_best, charge_window_best, discharge_window_best, discharge_limit_best, pv10, end_record=(my_predbat.end_record), save="test")
        plot(name, prediction)
    return failed


def run_window_sort_test(
    name, my_predbat, charge_window_best, discharge_window_best, expected=[], inverter_loss=1.0, metric_battery_cycle=0.0, battery_loss=1.0, battery_loss_discharge=1.0
):
    failed = False
    end_record = my_predbat.forecast_minutes
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_discharge = True
    my_predbat.inverter_loss = inverter_loss
    my_predbat.metric_battery_cycle = metric_battery_cycle
    my_predbat.battery_loss = battery_loss
    my_predbat.battery_loss_discharge = battery_loss_discharge

    print("Starting window sort test {}".format(name))

    record_charge_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, charge_window_best), 1)
    record_discharge_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, discharge_window_best), 1)

    window_sorted, window_index, price_set, price_links = my_predbat.sort_window_by_price_combined(
        charge_window_best[:record_charge_windows], discharge_window_best[:record_discharge_windows]
    )

    results = []
    for price_key in price_set:
        links = price_links[price_key]
        for key in links:
            typ = window_index[key]["type"]
            window_n = window_index[key]["id"]
            price = window_index[key]["average"]
            results.append((str(typ) + "_" + str(window_n) + "_" + str(price)))

    if len(expected) != len(results):
        print("ERROR: Expected {} results but got {}".format(len(expected), len(results)))
        failed = True
    else:
        for n in range(len(expected)):
            if expected[n] != results[n]:
                print("ERROR: Expected item {} is {} but got {}".format(n, expected[n], results[n]))
                failed = True
    if failed:
        print("Inputs: {} {}".format(charge_window_best, discharge_window_best))
        print("Results: {}".format(results))

    return failed


def run_window_sort_tests(my_predbat):
    import_rate = 10.0
    export_rate = 5.0
    reset_inverter(my_predbat)
    reset_rates(my_predbat, import_rate, export_rate)
    failed = False

    failed |= run_window_sort_test("none", my_predbat, [], [], expected=[])

    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": import_rate}]
    failed |= run_window_sort_test("single_charge", my_predbat, charge_window_best, [], expected=["c_0_10.0"])
    failed |= run_window_sort_test("single_charge_loss", my_predbat, charge_window_best, [], expected=["c_0_20.0"], inverter_loss=0.5)

    discharge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": export_rate}]
    failed |= run_window_sort_test("single_discharge", my_predbat, [], discharge_window_best, expected=["d_0_5.0"])
    failed |= run_window_sort_test("single_charge_discharge", my_predbat, charge_window_best, discharge_window_best, expected=["c_0_10.0", "d_0_5.0"])
    failed |= run_window_sort_test("single_charge_discharge_loss", my_predbat, charge_window_best, discharge_window_best, expected=["c_0_20.0", "d_0_2.5"], inverter_loss=0.5)
    discharge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 50.0}]
    failed |= run_window_sort_test("single_charge_discharge_loss2", my_predbat, charge_window_best, discharge_window_best, expected=["d_0_25.0", "c_0_20.0"], inverter_loss=0.5)
    failed |= run_window_sort_test(
        "single_charge_discharge_loss3", my_predbat, charge_window_best, discharge_window_best, expected=["c_0_200.0", "d_0_25.0"], inverter_loss=0.5, battery_loss=0.1
    )
    failed |= run_window_sort_test(
        "single_charge_discharge_loss4",
        my_predbat,
        charge_window_best,
        discharge_window_best,
        expected=["c_0_200.0", "d_0_2.5"],
        inverter_loss=0.5,
        battery_loss=0.1,
        battery_loss_discharge=0.1,
    )

    charge_window_best.append({"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": import_rate * 2})
    failed |= run_window_sort_test(
        "single_charge_discharge2",
        my_predbat,
        charge_window_best,
        discharge_window_best,
        expected=["c_1_400.0", "c_0_200.0", "d_0_2.5"],
        inverter_loss=0.5,
        battery_loss=0.1,
        battery_loss_discharge=0.1,
    )
    discharge_window_best.append({"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": export_rate * 3})
    failed |= run_window_sort_test("single_charge_discharge3", my_predbat, charge_window_best, discharge_window_best, expected=["d_0_50.0", "c_1_20.0", "d_1_15.0", "c_0_10.0"])
    failed |= run_window_sort_test(
        "single_charge_discharge3_c1", my_predbat, charge_window_best, discharge_window_best, expected=["d_0_49.0", "c_1_21.0", "d_1_14.0", "c_0_11.0"], metric_battery_cycle=1.0
    )

    return failed


def run_optimise_levels_tests(my_predbat):
    print("**** Running Optimise levels tests ****")
    reset_inverter(my_predbat)
    failed = False

    # Single charge window
    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}]
    failed |= run_optimise_levels(
        "single",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=[0],
        load_amount=1.0,
        pv_amount=0,
        expect_best_price=10.0 / 0.9,
        inverter_loss=0.9,
        expect_metric=10 * 24,
    )
    failed |= run_optimise_levels(
        "single_pv",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=[0],
        load_amount=1.0,
        pv_amount=1.0,
        expect_best_price=10.0 / 0.9,
        inverter_loss=0.9,
        expect_metric=0,
    )

    # Two windows charge low + high rate
    charge_window_best.append({"start": my_predbat.minutes_now + 120, "end": my_predbat.minutes_now + 240, "average": 5})
    # failed |= run_optimise_levels("dual", my_predbat, charge_window_best=charge_window_best, expect_charge_limit=[0, 100], load_amount=1.0, pv_amount=0, expect_best_price=5.0 / 0.9, inverter_loss=0.9)
    # failed |= run_optimise_levels("dual_pv", my_predbat, charge_window_best=charge_window_best, expect_charge_limit=[0, 100], load_amount=1.0, pv_amount=1.0, expect_best_price=5.0 / 0.9, inverter_loss=0.9)

    # Discharge
    discharge_window_best = [{"start": my_predbat.minutes_now + 240, "end": my_predbat.minutes_now + 300, "average": 7.5}]
    # failed |= run_optimise_levels("discharge", my_predbat, charge_window_best=charge_window_best, discharge_window_best=discharge_window_best, expect_charge_limit=[0, 100], expect_discharge_limit=[0], load_amount=0, pv_amount=0, expect_best_price=5.0, inverter_loss=1, expect_metric=-2.5)
    # failed |= run_optimise_levels("discharge_loss", my_predbat, charge_window_best=charge_window_best, discharge_window_best=discharge_window_best, expect_charge_limit=[0, 100], expect_discharge_limit=[0], load_amount=0, pv_amount=0, expect_best_price=5.0 / 0.9, inverter_loss=0.9)

    return failed


def run_optimise_levels(
    name,
    my_predbat,
    charge_window_best=[],
    discharge_window_best=[],
    pv_amount=0,
    load_amount=0,
    expect_charge_limit=[],
    expect_discharge_limit=[],
    expect_best_price=0.0,
    rate_import=10.0,
    rate_export=5.0,
    battery_size=100.0,
    battery_soc=0.0,
    hybrid=False,
    inverter_loss=1.0,
    expect_metric=0.0,
):
    end_record = my_predbat.forecast_minutes
    failed = False
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_discharge = True
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.inverter_hybrid = hybrid
    my_predbat.inverter_loss = inverter_loss

    reset_rates(my_predbat, rate_import, rate_export)
    update_rates_import(my_predbat, charge_window_best)
    update_rates_export(my_predbat, discharge_window_best)

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = pv_amount / (60 / 5)
        load_step[minute] = load_amount / (60 / 5)
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.debug_enable = True

    charge_limit_best = [0 for n in range(len(charge_window_best))]
    discharge_limits_best = [100 for n in range(len(discharge_window_best))]

    record_charge_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, charge_window_best), 1)
    record_discharge_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, discharge_window_best), 1)
    print("Starting optimise levels test {}".format(name))

    window_sorted, window_index, price_set, price_links = my_predbat.sort_window_by_price_combined(
        charge_window_best[:record_charge_windows], discharge_window_best[:record_discharge_windows]
    )

    my_predbat.optimise_charge_windows_reset(reset_all=True)
    my_predbat.optimise_charge_windows_manual()
    (
        charge_limit_best,
        discharge_limits_best,
        best_price,
        best_price_discharge,
        best_metric,
        best_cost,
        best_keep,
        best_soc_min,
        best_cycle,
        best_carbon,
        best_import,
        tried_list,
    ) = my_predbat.optimise_charge_limit_price_threads(
        price_set,
        price_links,
        window_index,
        record_charge_windows,
        record_discharge_windows,
        charge_limit_best,
        charge_window_best,
        discharge_window_best,
        discharge_limits_best,
        end_record=end_record,
        fast=True,
        quiet=True,
    )
    if len(expect_charge_limit) != len(charge_limit_best):
        print("ERROR: Expected {} charge limits but got {}".format(len(expect_charge_limit), len(charge_limit_best)))
        failed = True
    else:
        for n in range(len(expect_charge_limit)):
            if expect_charge_limit[n] != charge_limit_best[n]:
                print("ERROR: Expected charge limit {} is {} but got {}".format(n, expect_charge_limit[n], charge_limit_best[n]))
                failed = True
    if len(expect_discharge_limit) != len(discharge_limits_best):
        print("ERROR: Expected {} discharge limits but got {}".format(len(expect_discharge_limit), len(discharge_limits_best)))
        failed = True
    else:
        for n in range(len(expect_discharge_limit)):
            if expect_discharge_limit[n] != discharge_limits_best[n]:
                print("ERROR: Expected discharge limit {} is {} but got {}".format(n, expect_discharge_limit[n], discharge_limits_best[n]))
                failed = True

    if abs(expect_best_price - best_price) >= 0.1:
        print("ERROR: Expected best price {} but got {}".format(expect_best_price, best_price))
        failed = True

    if abs(expect_metric - best_metric) >= 0.1:
        print("ERROR: Expected best metric {} but got {}".format(expect_metric, best_metric))
        failed = True

    if failed:
        old_log = my_predbat.log
        my_predbat.log = print
        my_predbat.optimise_charge_limit_price_threads(
            price_set,
            price_links,
            window_index,
            record_charge_windows,
            record_discharge_windows,
            charge_limit_best,
            charge_window_best,
            discharge_window_best,
            discharge_limits_best,
            end_record=end_record,
            fast=True,
            quiet=True,
            test_mode=True,
        )
        my_predbat.log = old_log
        print(
            "Best price: {} Best metric: {} Best cost: {} Best keep: {} Best soc min: {} Best cycle: {} Best carbon: {} Best import: {}".format(
                best_price, best_metric, best_cost, best_keep, best_soc_min, best_cycle, best_carbon, best_import
            )
        )
        print("Charge limit best: {} expected {} Discharge limit best {} expected {}".format(charge_limit_best, expect_charge_limit, discharge_limits_best, expect_discharge_limit))

    return failed


def run_perf_test(my_predbat):
    print("**** Running Performance tests ****")
    reset_inverter(my_predbat)
    import_rate = 10.0
    export_rate = 5.0
    reset_rates(my_predbat, import_rate, export_rate)
    failed = False

    start_time = time.time()
    for count in range(0, 50):
        failed |= simple_scenario(
            "load_bat_dc_pv2",
            my_predbat,
            4,
            4,
            assert_final_metric=import_rate * 24 * 3.2,
            assert_final_soc=50 + 24,
            with_battery=True,
            battery_soc=50.0,
            inverter_loss=0.8,
            hybrid=True,
            quiet=True,
            save="none",
        )
    end_time = time.time()
    if failed:
        print("Performance test failed")

    run_time = end_time - start_time
    print("Performance test took {} seconds for 50 iterations = {} iterations per second".format(run_time, round(1 / (run_time / 50.0), 2)))
    return failed


def run_model_tests(my_predbat):
    print("**** Running Model tests ****")
    reset_inverter(my_predbat)
    import_rate = 10.0
    export_rate = 5.0
    low_rates = reset_rates(my_predbat, import_rate, export_rate)

    failed = False
    failed |= simple_scenario("zero", my_predbat, 0, 0, 0, 0, with_battery=False)
    failed |= simple_scenario("load_only", my_predbat, 1, 0, assert_final_metric=import_rate * 24, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario(
        "load_bat_ac", my_predbat, 4, 0, assert_final_metric=import_rate * 24 * 3.2, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8
    )
    failed |= simple_scenario(
        "load_bat_dc", my_predbat, 4, 0, assert_final_metric=import_rate * 24 * 3.2, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8, hybrid=True
    )
    failed |= simple_scenario(
        "load_bat_dc_pv",
        my_predbat,
        4,
        0.5,
        assert_final_metric=import_rate * 24 * 3.2,
        assert_final_soc=100 - 24 * 0.5,
        with_battery=True,
        battery_soc=100.0,
        inverter_loss=0.8,
        hybrid=True,
    )
    failed |= simple_scenario(
        "load_bat_dc_pv2",
        my_predbat,
        4,
        4,
        assert_final_metric=import_rate * 24 * 3.2,
        assert_final_soc=50 + 24,
        with_battery=True,
        battery_soc=50.0,
        inverter_loss=0.8,
        hybrid=True,
    )
    failed |= simple_scenario("load_carbon", my_predbat, 1, 0, assert_final_metric=import_rate * 24, assert_final_soc=0, with_battery=False, carbon=3, assert_final_carbon=3 * 24)
    failed |= simple_scenario(
        "load_carbon_loss_ac",
        my_predbat,
        1,
        0,
        assert_final_metric=import_rate * 24,
        assert_final_soc=0,
        with_battery=False,
        carbon=3,
        assert_final_carbon=3 * 24,
        inverter_limit=3.0,
        inverter_loss=0.8,
    )
    failed |= simple_scenario(
        "load_carbon_loss_dc",
        my_predbat,
        1,
        0,
        assert_final_metric=import_rate * 24,
        assert_final_soc=0,
        with_battery=False,
        carbon=3,
        assert_final_carbon=3 * 24,
        inverter_limit=3.0,
        inverter_loss=0.8,
        hybrid=True,
    )
    failed |= simple_scenario(
        "pv_carbon_ac",
        my_predbat,
        0,
        1,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=0,
        with_battery=False,
        carbon=3,
        assert_final_carbon=-3 * 24,
        inverter_limit=3.0,
        inverter_loss=0.8,
    )
    failed |= simple_scenario(
        "pv_carbon_dc",
        my_predbat,
        0,
        1,
        assert_final_metric=-export_rate * 24 * 0.8,
        assert_final_soc=0,
        with_battery=False,
        carbon=3,
        assert_final_carbon=-3 * 24 * 0.8,
        inverter_limit=3.0,
        inverter_loss=0.8,
        hybrid=True,
    )
    failed |= simple_scenario("load_car", my_predbat, 1, 0, assert_final_metric=import_rate * 24 * 3, assert_final_soc=0, with_battery=False, charge_car=2.0)
    failed |= simple_scenario(
        "load_car_bat_yes", my_predbat, 1, 0, assert_final_metric=import_rate * 24 * 2, assert_final_soc=100.0 - 24 * 1, with_battery=True, charge_car=2.0, battery_soc=100.0
    )
    failed |= simple_scenario(
        "load_car_bat_no",
        my_predbat,
        1,
        0,
        assert_final_metric=import_rate * 24 * 3,
        assert_final_soc=100.0,
        with_battery=True,
        charge_car=2.0,
        battery_soc=100.0,
        car_charging_from_battery=False,
    )
    failed |= simple_scenario(
        "load_car_bat_no2",
        my_predbat,
        1,
        0,
        assert_final_metric=0,
        assert_final_soc=100.0 - 24,
        with_battery=True,
        charge_car=0,
        battery_soc=100.0,
        car_charging_from_battery=False,
    )
    failed |= simple_scenario("load_discharge", my_predbat, 1, 0, assert_final_metric=import_rate * 14, assert_final_soc=0, battery_soc=10.0, with_battery=True)
    failed |= simple_scenario("load_discharge2", my_predbat, 1, 0, assert_final_metric=0, assert_final_soc=100 - 24, battery_soc=100.0, with_battery=True)
    failed |= simple_scenario("load_discharge3", my_predbat, 1, 0, assert_final_metric=0, assert_final_soc=100 - 48, battery_soc=100.0, with_battery=True, battery_loss=0.5)
    failed |= simple_scenario("load_discharge4", my_predbat, 1, 0, assert_final_metric=import_rate * 14, assert_final_soc=0, battery_soc=100.0, with_battery=True, battery_loss=0.1)
    failed |= simple_scenario(
        "load_discharge_car",
        my_predbat,
        0.5,
        0,
        assert_final_metric=import_rate * 14 * 4.5 + import_rate * 10 * 3.5,
        assert_final_soc=0,
        battery_soc=10.0,
        with_battery=True,
        charge_car=4.0,
    )
    failed |= simple_scenario(
        "load_discharge_car2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 24 * 1.5,
        assert_final_soc=100 - 24 * 2.5,
        battery_soc=100.0,
        with_battery=True,
        charge_car=4.0,
        discharge=0,
        inverter_limit=3.5,
        battery_rate_max_charge=2.5,
    )
    failed |= simple_scenario("load_discharge_fast", my_predbat, 2, 0, assert_final_metric=import_rate * 38, assert_final_soc=0, battery_soc=10.0, with_battery=True)
    failed |= simple_scenario("load_discharge_fast_big", my_predbat, 2, 0, assert_final_metric=import_rate * 24, assert_final_soc=76, battery_soc=100.0, with_battery=True)
    failed |= simple_scenario(
        "load_discharge_reserve", my_predbat, 1, 0, assert_final_metric=import_rate * 15, assert_final_soc=1, battery_soc=10.0, with_battery=True, reserve=1.0
    )
    failed |= simple_scenario(
        "load_discharge_reserve2", my_predbat, 1, 0, assert_final_metric=import_rate * 20, assert_final_soc=2, battery_soc=10.0, with_battery=True, reserve=2.0, battery_loss=0.5
    )
    failed |= simple_scenario(
        "load_discharge_loss", my_predbat, 1, 0, assert_final_metric=import_rate * 19, assert_final_soc=0, battery_soc=10.0, with_battery=True, battery_loss=0.5
    )
    failed |= simple_scenario("load_pv", my_predbat, 1, 1, assert_final_metric=0, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("pv_only", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("pv10_only", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False, pv10=True)
    failed |= simple_scenario("pv_only_loss_ac", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False, inverter_loss=0.5)
    failed |= simple_scenario(
        "pv_only_loss_hybrid", my_predbat, 0, 1, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=0, with_battery=False, inverter_loss=0.5, hybrid=True
    )
    failed |= simple_scenario("pv_only_bat", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_loss", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=12, with_battery=True, battery_loss=0.5)
    failed |= simple_scenario("pv_only_bat_100%", my_predbat, 0, 1, assert_final_metric=-export_rate * 14, assert_final_soc=10, with_battery=True, battery_size=10)
    failed |= simple_scenario("pv_only_bat_ac_clips2", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_ac_clips3", my_predbat, 0, 3, assert_final_metric=-export_rate * 48, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario(
        "pv_only_bat_ac_export_limit", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, export_limit=0.5
    )
    failed |= simple_scenario(
        "pv_only_bat_ac_export_limit_loss",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24 * 0.1,
        assert_final_soc=12,
        with_battery=True,
        export_limit=0.1,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "pv_only_bat_ac_export_limit_load", my_predbat, 0.5, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, export_limit=0.5
    )
    failed |= simple_scenario("pv_only_bat_dc_clips2", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, hybrid=True)
    failed |= simple_scenario(
        "pv_only_bat_dc_clips2l", my_predbat, 0, 2, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, inverter_loss=0.5
    )
    failed |= simple_scenario("pv_only_bat_dc_clips3", my_predbat, 0, 3, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, hybrid=True)
    failed |= simple_scenario(
        "pv_only_bat_dc_clips3l", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, inverter_loss=0.5
    )
    failed |= simple_scenario(
        "pv_only_bat_dc_clips3l2",
        my_predbat,
        0,
        3,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=24,
        with_battery=True,
        hybrid=True,
        inverter_loss=0.5,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "pv_only_bat_dc_export_limit", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, export_limit=0.5
    )
    failed |= simple_scenario(
        "pv_only_bat_dc_export_limit_loss",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24 * 0.1,
        assert_final_soc=24,
        with_battery=True,
        hybrid=True,
        export_limit=0.1,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "pv_only_bat_dc_export_limit_load", my_predbat, 0.5, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, export_limit=0.5
    )
    failed |= simple_scenario("battery_charge", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario(
        "battery_charge_freeze",
        my_predbat,
        0.5,
        0,
        assert_final_metric=import_rate * 24 * 0.5,
        assert_final_soc=5,
        with_battery=True,
        charge=0.5,
        battery_soc=5,
        battery_size=10,
        reserve=0.5,
    )
    failed |= simple_scenario(
        "battery_charge_freeze2",
        my_predbat,
        0.5,
        1,
        assert_final_metric=0,
        assert_final_soc=5 + 0.5 * 24,
        with_battery=True,
        charge=0.5,
        battery_soc=5,
        battery_size=100,
        reserve=0.5,
    )
    failed |= simple_scenario("battery_charge_load", my_predbat, 1, 0, assert_final_metric=import_rate * 34, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario(
        "battery_charge_load2", my_predbat, 2, 0, assert_final_metric=import_rate * (34 + 24), assert_final_soc=10, with_battery=True, charge=10, battery_size=10
    )
    failed |= simple_scenario("battery_charge_pv", my_predbat, 0, 1, assert_final_metric=-export_rate * 14, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario("battery_charge_pv2", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24, with_battery=True, charge=100, battery_size=100)
    failed |= simple_scenario("battery_charge_pv3", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, charge=100, battery_size=100)
    failed |= simple_scenario(
        "battery_charge_pv4_ac", my_predbat, 0, 2, assert_final_metric=0, assert_final_soc=24, with_battery=True, charge=100, battery_size=100, inverter_loss=0.5, inverter_limit=2
    )
    failed |= simple_scenario(
        "battery_charge_pv4_dc",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_pv5_ac",
        my_predbat,
        0,
        3,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
    )
    failed |= simple_scenario(
        "battery_charge_pv5_dc",
        my_predbat,
        0,
        3,
        assert_final_metric=-export_rate * 24 * 1,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_pv6_ac",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24 * 2,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
    )
    failed |= simple_scenario(
        "battery_charge_pv6_dc",
        my_predbat,
        0,
        4,
        assert_final_metric=-export_rate * 24 * 1,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_pv_term_dc1",
        my_predbat,
        0,
        0.5,
        assert_final_metric=import_rate * 10 * 0.5,
        assert_final_soc=10 + 14 * 0.5,
        with_battery=True,
        charge=10,
        battery_size=100,
        hybrid=True,
        assert_keep=0,
    )
    failed |= simple_scenario(
        "battery_charge_pv_term_dc2",
        my_predbat,
        0,
        0.5,
        assert_final_metric=import_rate * 10 * 0.5,
        assert_final_soc=10 + 14 * 0.5,
        with_battery=True,
        charge=9.95,
        battery_size=100,
        hybrid=True,
        assert_keep=((1 / 60 * 5) - 0.05) * import_rate,
    )
    failed |= simple_scenario(
        "battery_charge_pv_load1",
        my_predbat,
        0.5,
        1,
        assert_final_metric=import_rate * 0.5 * 10 - export_rate * 14 * 0.5,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
    )
    failed |= simple_scenario(
        "battery_charge_pv_load2_ac", my_predbat, 0.5, 1, assert_final_metric=import_rate * 0.5 * 24, assert_final_soc=24, with_battery=True, charge=100, battery_soc=0
    )
    failed |= simple_scenario(
        "battery_charge_pv_load2_hybrid",
        my_predbat,
        0.5,
        1,
        assert_final_metric=import_rate * 0.5 * 24,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_soc=0,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_pv_load3_ac", my_predbat, 0.5, 2, assert_final_metric=-export_rate * 0.5 * 24, assert_final_soc=24, with_battery=True, charge=100, battery_soc=0
    )
    failed |= simple_scenario(
        "battery_charge_pv_load3_hybrid",
        my_predbat,
        0.5,
        2,
        assert_final_metric=-export_rate * 0.5 * 24,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_soc=0,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_part1",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 1,
        assert_final_soc=1,
        with_battery=True,
        charge=10,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 120, "average": 10}],
    )
    failed |= simple_scenario(
        "battery_charge_part1.5",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 1.5,
        assert_final_soc=1.5,
        with_battery=True,
        charge=10,
        battery_size=10,
        charge_window_best=[{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 150, "average": 10}],
    )
    failed |= simple_scenario("battery_discharge", my_predbat, 0, 0, assert_final_metric=-export_rate * 10, assert_final_soc=0, with_battery=True, discharge=0, battery_soc=10)
    failed |= simple_scenario(
        "battery_discharge_keep",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 10,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        assert_keep=1 * import_rate * KEEP_SCALE,
        keep=1,
    )
    failed |= simple_scenario(
        "battery_discharge_loss",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 10 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_loss2",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24 * 0.25,
        assert_final_soc=100 - 24 * 0.5,
        battery_soc=100.0,
        with_battery=True,
        inverter_loss=0.5,
        discharge=0,
        inverter_limit=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_load",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_load_keep",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        assert_keep=14 * import_rate * 0.5 * KEEP_SCALE + 1 * import_rate * KEEP_SCALE,
        keep=1.0,
    )
    failed |= simple_scenario(
        "battery_discharge_load_keep_mode_test1",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        assert_keep=14 * import_rate * 0.5 * KEEP_SCALE + 1 * import_rate * KEEP_SCALE,
        keep=1.0,
        save="test",
    )
    failed |= simple_scenario(
        "battery_discharge_load_keep_mode_test2",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        assert_keep=14 * import_rate * 0.5 * KEEP_SCALE + 1 * import_rate * KEEP_SCALE,
        keep=1.0,
        save="none",
    )
    failed |= simple_scenario(
        "battery_discharge_pv_ac",
        my_predbat,
        0,
        0.5,
        assert_final_metric=-export_rate * 10 - export_rate * 24 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv_ac_load",
        my_predbat,
        0.1,
        0.5,
        assert_final_metric=-export_rate * 9 - export_rate * 24 * 0.4,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv2_ac",
        my_predbat,
        0,
        1.5,
        assert_final_metric=-export_rate * 10 * 2.5 - export_rate * 14 * 1.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv3_ac",
        my_predbat,
        0,
        2.0,
        assert_final_metric=-export_rate * 10 * 3 - export_rate * 14 * 2,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv4_ac",
        my_predbat,
        0,
        5.0,
        assert_final_metric=-export_rate * 10 * 6 - export_rate * 14 * 5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
    )
    failed |= simple_scenario(
        "battery_discharge_pv5_ac",
        my_predbat,
        1,
        5.0,
        assert_final_metric=-export_rate * 24 * 4.5,
        assert_final_soc=50 - 24 * 1,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        inverter_limit=2,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_pv_hybrid",
        my_predbat,
        0,
        0.5,
        assert_final_metric=-export_rate * 20 - export_rate * 4 * 0.5,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=10,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_discharge_pv2_hybrid", my_predbat, 0, 1.5, assert_final_metric=-export_rate * 24, assert_final_soc=22, with_battery=True, discharge=0, battery_soc=10, hybrid=True
    )
    failed |= simple_scenario(
        "battery_discharge_pv3_hybrid", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, discharge=0, battery_soc=0, hybrid=True
    )
    failed |= simple_scenario(
        "battery_discharge_pv4_hybrid",
        my_predbat,
        1,
        5,
        assert_final_metric=0,
        assert_final_soc=50 + 1 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        hybrid=True,
        inverter_limit=2,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_freeze", my_predbat, 0, 0.5, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=10, with_battery=True, discharge=99, battery_soc=10
    )
    failed |= simple_scenario("battery_discharge_hold", my_predbat, 0, 0.5, assert_final_metric=-0, assert_final_soc=10 + 24 * 0.5, with_battery=True, discharge=98, battery_soc=10)
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 - 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv2",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv3",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 1.0 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv4",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 1.0 * 24 * 0.5,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_ac_pv5",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 1.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        inverter_limit=2.0,
        battery_rate_max_charge=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 - 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 0.0,
        assert_final_soc=50 + 1 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv2",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 0.5 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv3",
        my_predbat,
        1,
        2,
        assert_final_metric=-export_rate * 24 * 1.0,
        assert_final_soc=50 + 0 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=1.0,
        hybrid=True,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_discharge_export_limit_hybrid_pv4",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=50 + 1 * 24,
        with_battery=True,
        discharge=0,
        battery_soc=50,
        export_limit=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_ac_loss",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 10 / 0.5,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_charge_hybrid_loss",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 10 / 0.5,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        inverter_loss=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_ac_loss_pv", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24 * 0.5, with_battery=True, charge=100, battery_size=100, inverter_loss=0.5
    )
    failed |= simple_scenario(
        "battery_charge_ac_loss_pv2",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=24 * 0.5,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
    )
    failed |= simple_scenario(
        "battery_charge_ac_loss_pv3",
        my_predbat,
        0,
        2,
        assert_final_metric=0,
        assert_final_soc=24 * 1,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        inverter_limit=2.0,
    )
    failed |= simple_scenario(
        "battery_charge_hybrid_loss_pv",
        my_predbat,
        0,
        1,
        assert_final_metric=0,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_hybrid_loss_pv2",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        hybrid=True,
    )
    failed |= simple_scenario(
        "battery_charge_hybrid_loss_pv3",
        my_predbat,
        0,
        2,
        assert_final_metric=-export_rate * 24 * 0.5,
        assert_final_soc=24,
        with_battery=True,
        charge=100,
        battery_size=100,
        inverter_loss=0.5,
        hybrid=True,
        inverter_limit=2.0,
    )
    failed |= simple_scenario("iboost_pv", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=0, with_battery=False, iboost_solar=True, assert_final_iboost=24)
    failed |= simple_scenario(
        "iboost_gas1",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_gas=True,
        rate_gas=5.0,
        gas_scale=0.8,
        iboost_charging=False,
        assert_final_iboost=0,
        low_rates=low_rates,
    )
    failed |= simple_scenario(
        "iboost_gas2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_gas=True,
        rate_gas=10.0,
        gas_scale=1.2,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        low_rates=low_rates,
    )
    failed |= simple_scenario(
        "iboost_rate1",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_rate=True,
        iboost_rate_threshold=import_rate * 0.9,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=0,
        low_rates=low_rates,
    )
    failed |= simple_scenario(
        "iboost_rate2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_rate=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        low_rates=low_rates,
    )
    failed |= simple_scenario(
        "iboost_charge1",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * (10 * 2 * 10 + 10),
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_charging=True,
        assert_final_iboost=200,
    )
    failed |= simple_scenario(
        "iboost_charge2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * (10 * 10 + 10),
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_charging=True,
        assert_final_iboost=100,
        end_record=12 * 60,
    )
    failed |= simple_scenario(
        "iboost_discharge1",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 10,
        assert_final_soc=0,
        battery_soc=10,
        with_battery=True,
        discharge=0,
        battery_size=10,
        iboost_charging=True,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "keep_discharge1",
        my_predbat,
        0.5,
        0,
        assert_final_metric=-export_rate * 10 * 0.5 + import_rate * 14 * 0.5,
        assert_final_soc=0,
        battery_soc=10,
        with_battery=True,
        discharge=0,
        battery_size=10,
        keep=1.0,
        assert_final_iboost=0,
        assert_keep=import_rate * 14 * 0.5 * KEEP_SCALE + import_rate * 1 * KEEP_SCALE,
    )

    # Alternating high/low rates
    low_rates = reset_rates2(my_predbat, import_rate, export_rate)
    failed |= simple_scenario(
        "iboost_rate3",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 120,
        assert_final_soc=0,
        with_battery=False,
        iboost_rate=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=120,
        low_rates=low_rates,
    )
    failed |= simple_scenario(
        "iboost_rate_pv1",
        my_predbat,
        0,
        1.0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_rate=True,
        iboost_solar=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=24,
        low_rates=low_rates,
        export_limit=1,
    )
    failed |= simple_scenario(
        "iboost_rate_pv2",
        my_predbat,
        0,
        1.0,
        assert_final_metric=12 * import_rate,
        assert_final_soc=0,
        with_battery=False,
        iboost_rate=True,
        iboost_solar=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=12 * 1 + 12 * 2,
        low_rates=low_rates,
        export_limit=2,
    )

    if failed:
        print("**** ERROR: Some Model tests failed ****")
    return failed


def main():
    print("**** Starting Predbat tests ****")
    my_predbat = PredBat()
    my_predbat.states = {}
    my_predbat.reset()
    my_predbat.update_time()
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.auto_config()
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    my_predbat.forecast_minutes = 24 * 60

    print("**** Testing Predbat ****")
    failed = False
    failed |= run_model_tests(my_predbat)
    failed |= run_window_sort_tests(my_predbat)
    failed |= run_optimise_levels_tests(my_predbat)
    failed |= run_compute_metric_tests(my_predbat)
    failed |= run_perf_test(my_predbat)

    if failed:
        print("**** ERROR: Some tests failed ****")
        sys.exit(1)
    print("**** Tests passed ****")
    sys.exit(0)


if __name__ == "__main__":
    main()
