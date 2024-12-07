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
import argparse

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
from utils import calc_percent_limit, remove_intersecting_windows
from futurerate import FutureRate

KEEP_SCALE = 0.5


class TestHAInterface:
    def __init__(self):
        self.step = 5
        self.build_history()
        self.history_enable = True
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
        if not entity_id:
            return {}
        else:
            return None

    def call_service(self, domain, service, data):
        print("Calling service: {} {}".format(domain, service))
        return None

    def set_state(self, entity_id, state, attributes=None):
        # print("Setting state: {} to {}".format(entity_id, state))
        return None

    def get_history(self, entity_id, now=None, days=30):
        print("Getting history for {}".format(entity_id))
        if self.history_enable:
            return [self.history]
        else:
            return None


class TestInverter:
    def __init__(self):
        pass


def reset_rates(my_predbat, ir, xr):
    my_predbat.combine_charge_slots = True
    for minute in range(my_predbat.forecast_minutes + my_predbat.minutes_now):
        my_predbat.rate_import[minute] = ir
        my_predbat.rate_export[minute] = xr
    my_predbat.rate_export_min = xr
    my_predbat.rate_import = my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_export = my_predbat.rate_scan(my_predbat.rate_export, print=False)


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
    my_predbat.rate_import = my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_export = my_predbat.rate_scan(my_predbat.rate_export, print=False)


def update_rates_import(my_predbat, charge_window_best):
    for window in charge_window_best:
        for minute in range(window["start"], window["end"]):
            my_predbat.rate_import[minute] = window["average"]
    my_predbat.rate_import = my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_export = my_predbat.rate_scan(my_predbat.rate_export, print=False)


def update_rates_export(my_predbat, export_window_best):
    for window in export_window_best:
        for minute in range(window["start"], window["end"]):
            my_predbat.rate_export[minute] = window["average"]
    my_predbat.rate_import = my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_export = my_predbat.rate_scan(my_predbat.rate_export, print=False)


def reset_inverter(my_predbat):
    my_predbat.inverter_limit = 1 / 60.0
    my_predbat.num_inverters = 1
    my_predbat.export_limit = 10 / 60.0
    my_predbat.inverters = [TestInverter()]
    my_predbat.charge_window = []
    my_predbat.export_window = []
    my_predbat.export_limits = []
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
    my_predbat.iboost_gas_export = False
    my_predbat.iboost_charging = False
    my_predbat.iboost_smart = False
    my_predbat.iboost_on_export = False
    my_predbat.iboost_prevent_discharge = False
    my_predbat.minutes_now = 12 * 60
    my_predbat.best_soc_keep = 0.0
    my_predbat.carbon_enable = 0
    my_predbat.inverter_soc_reset = True
    my_predbat.car_charging_soc_next = [None for car_n in range(4)]


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


def run_nordpool_test(my_predbat):
    """
    Test the compute metric function
    """
    print("**** Running Nordpool tests ****")
    my_predbat.args["futurerate_url"] = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices?date=DATE&market=N2EX_DayAhead&deliveryArea=UK&currency=GBP"
    my_predbat.args["futurerate_adjust_import"] = False
    my_predbat.args["futurerate_adjust_export"] = False
    my_predbat.args["futurerate_peak_start"] = "16:00:00"
    my_predbat.args["futurerate_peak_end"] = "19:00:00"
    my_predbat.args["futurerate_peak_premium_import"] = 14
    my_predbat.args["futurerate_peak_premium_export"] = 6.5
    my_predbat.args["futurerate_adjust_import"] = True
    my_predbat.args["futurerate_adjust_export"] = True
    failed = False

    # Obtain Agile octopus data
    rates_agile = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/AGILE-FLEX-BB-23-02-08/electricity-tariffs/E-1R-AGILE-FLEX-BB-23-02-08-A/standard-unit-rates")
    if not rates_agile:
        print("ERROR: No import rate data from Octopus url {}".format(url))
        failed = True
    rates_agile_export = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/")
    if not rates_agile_export:
        print("ERROR: No export rate data from Octopus url {}".format(url))
        failed = True
    print("Agile rates downloaded...")

    future = FutureRate(my_predbat)
    rate_import, rate_export = future.futurerate_analysis(rates_agile, rates_agile_export)
    if not rate_import:
        print("ERROR: No rate import data")
        return True
    if not rate_export:
        print("ERROR: No rate export data")
        return True

    future.download_futurerate_data_func = lambda x: ("empty")  # Mock the download function
    rate_import2, rate_export2 = future.futurerate_analysis(rates_agile, rates_agile_export)
    for key in rate_import:
        if rate_import[key] != rate_import2.get(key, None):
            print("ERROR: Rate import data not the same")
            failed = True
    for key in rate_export:
        if rate_export[key] != rate_export2.get(key, None):
            print("ERROR: Rate export data not the same")
            failed = True

    # Compute the minimum value in the hash, ignoring the keys
    min_import = min(rate_import.values())
    min_export = min(rate_export.values())
    max_import = max(rate_import.values())
    max_export = max(rate_export.values())

    if min_import == max_import:
        print("ERROR: Rate import data is flat")
        failed = True
    if min_export == max_export:
        print("ERROR: Rate import data is flat")
        failed = True
    if min_import < -15 or max_import > 100:
        print("ERROR: Rate import data out of range got min {} max {}".format(min_import, max_import))
        failed = True
    if min_export < 0 or max_export > 50:
        print("ERROR: Rate export data out of range got min {} max {}".format(min_export, max_export))
        failed = True

    # Compare Agile rates against Nordpool
    max_diff = 0
    for minute in range(0, 24 * 60, 30):
        rate_octopus = rates_agile.get(minute, None)
        rate_nordpool = rate_import.get(minute, None)
        if rate_octopus is not None and rate_nordpool is not None:
            rate_diff = abs(rate_octopus - rate_nordpool)
            max_diff = max(max_diff, rate_diff)
            # print("Import: Minute {} Octopus {} Nordpool {} diff {}".format(my_predbat.time_abs_str(minute), rate_octopus, rate_nordpool, rate_diff))
    if max_diff > 10:
        print("ERROR: Rate import data difference too high")
        failed = True

    rate_diff_export = 0
    for minute in range(0, 24 * 60, 30):
        rate_octopus = rates_agile_export.get(minute, None)
        rate_nordpool = rate_export.get(minute, None)
        if rate_octopus is not None and rate_nordpool is not None:
            rate_diff_export = abs(rate_octopus - rate_nordpool)
            max_diff = max(rate_diff_export, rate_diff)
            # print("Export: Minute {} Octopus {} Nordpool {} diff {}".format(my_predbat.time_abs_str(minute), rate_octopus, rate_nordpool, rate_diff))
    if rate_diff_export > 10:
        print("ERROR: Rate export data difference too high")
        failed = True

    return failed


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
    charge_period_divide=1,
    discharge=100,
    charge_window_best=[],
    inverter_loss=1.0,
    battery_rate_max_charge=1.0,
    charge_car=0,
    car_charging_from_battery=True,
    iboost_solar=False,
    iboost_gas=False,
    iboost_gas_export=False,
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
    iboost_rate_threshold=9999,
    iboost_rate_threshold_export=9999,
    iboost_smart=False,
    iboost_enable=False,
    iboost_on_export=False,
    iboost_prevent_discharge=False,
    assert_iboost_running=False,
    assert_iboost_running_solar=False,
    assert_iboost_running_full=False,
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

    my_predbat.iboost_enable = iboost_enable
    my_predbat.iboost_gas = iboost_gas
    my_predbat.iboost_gas_export = iboost_gas_export
    my_predbat.iboost_solar = iboost_solar
    my_predbat.iboost_smart = iboost_smart
    my_predbat.iboost_rate_threshold = iboost_rate_threshold
    my_predbat.iboost_rate_threshold_export = iboost_rate_threshold_export
    my_predbat.iboost_min_power = 0.0
    my_predbat.iboost_max_power = export_limit / 60.0
    my_predbat.iboost_max_energy = iboost_max_energy
    my_predbat.iboost_on_export = iboost_on_export
    my_predbat.iboost_prevent_discharge = iboost_prevent_discharge
    my_predbat.rate_gas = {n: rate_gas for n in range(my_predbat.forecast_minutes + my_predbat.minutes_now)}
    my_predbat.iboost_gas_scale = gas_scale
    my_predbat.iboost_charging = iboost_charging
    my_predbat.best_soc_keep = keep
    my_predbat.car_charging_soc[0] = 0
    my_predbat.car_charging_limit[0] = 100.0

    if my_predbat.iboost_enable and (((not iboost_solar) and (not iboost_charging)) or iboost_smart):
        my_predbat.iboost_plan = my_predbat.plan_iboost_smart()
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
        my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now, "end": my_predbat.forecast_minutes + my_predbat.minutes_now, "kwh": charge_car * my_predbat.forecast_minutes / 60.0}]
    else:
        my_predbat.num_cars = 0
        my_predbat.car_charging_slots[0] = []

    prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step)

    charge_limit_best = []
    if charge > 0:
        charge_limit_best = [charge]
        if not charge_window_best:
            charge_window_best = [{"start": my_predbat.minutes_now, "end": int(my_predbat.forecast_minutes / charge_period_divide) + my_predbat.minutes_now, "average": 0}]
    export_limit_best = []
    export_window_best = []
    if discharge < 100:
        export_limit_best = [discharge]
        export_window_best = [{"start": my_predbat.minutes_now, "end": int(my_predbat.forecast_minutes / charge_period_divide) + my_predbat.minutes_now, "average": 0}]
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
        ) = wrapped_run_prediction_single(charge_limit_best, charge_window_best, export_window_best, export_limit_best, pv10, end_record=(my_predbat.end_record), step=5)
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
        ) = prediction.run_prediction(charge_limit_best, charge_window_best, export_window_best, export_limit_best, pv10, end_record=(my_predbat.end_record), save=save)
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
    if assert_iboost_running != prediction.iboost_running:
        print("ERROR: iBoost running should be {}".format(assert_iboost_running))
        failed = True
    if assert_iboost_running_solar != prediction.iboost_running_solar:
        print("ERROR: iBoost running solar should be {}".format(assert_iboost_running_solar))
        failed = True
    if assert_iboost_running_full != prediction.iboost_running_full:
        print("ERROR: iBoost running full should be {}".format(assert_iboost_running_full))
        failed = True

    if failed:
        prediction.run_prediction(charge_limit_best, charge_window_best, export_window_best, export_limit_best, pv10, end_record=(my_predbat.end_record), save="test")
        plot(name, prediction)
    return failed


class DummyInverter:
    def __init__(self, log, inverter_id=0):
        self.soc_kw = 0
        self.soc_max = 100
        self.soc_percent = 0
        self.battery_rate_max_charge = 1.0
        self.log = log
        self.id = inverter_id
        self.count_register_writes = 0

    def adjust_battery_target(self, soc, isCharging=False, isExporting=False):
        self.soc_target = soc
        self.isCharging = isCharging
        self.isExporting = isExporting


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
    inverter.soc_max = soc_max
    inverter.soc_kw = soc_kw
    inverter.soc_percent = calc_percent_limit(soc_kw, soc_max)
    my_predbat.soc_max = soc_max_all
    my_predbat.soc_kw = soc_kw_all
    my_predbat.battery_rate_max_charge = battery_rate_max_charge_all

    my_predbat.adjust_battery_target_multi(inverter, soc, isCharging, isExporting)
    if assert_soc != inverter.soc_target:
        print("ERROR: SOC {} should be {}".format(inverter.soc_target, assert_soc))
        failed = True
    if isCharging != inverter.isCharging:
        print("ERROR: isCharging {} should be {}".format(inverter.isCharging, isCharging))
        failed = True
    return failed


class ActiveTestInverter:
    def __init__(self, id, soc_kw, soc_max, now_utc):
        self.soc_target = -1
        self.id = id
        self.isCharging = False
        self.isExporting = False
        self.pause_charge = False
        self.pause_discharge = False
        self.reserve = -1
        self.idle_charge_start = -1
        self.idle_charge_end = -1
        self.idle_discharge_start = -1
        self.idle_discharge_end = -1
        self.force_export = False
        self.discharge_start_time_minutes = -1
        self.discharge_end_time_minutes = -1
        self.immediate_charge_soc_target = -1
        self.immediate_discharge_soc_target = -1
        self.immediate_charge_soc_freeze = False
        self.immediate_discharge_soc_freeze = False
        self.charge_start_time_minutes = -1
        self.charge_end_time_minutes = -1
        self.charge_rate = 1000
        self.discharge_rate = 1000
        self.charge_time_enable = False
        self.in_calibration = False
        self.inv_charge_discharge_with_rate = False
        self.inv_can_span_midnight = True
        self.inv_has_target_soc = True
        self.inv_has_charge_enable_time = True
        self.inv_has_timed_pause = True
        self.soc_kw = soc_kw
        self.soc_max = soc_max
        self.soc_percent = calc_percent_limit(soc_kw, soc_max)
        self.battery_rate_max_charge = 1 / 60.0
        self.battery_rate_max_charge_scaled = 1 / 60.0
        self.battery_rate_max_discharge = 1 / 60.0
        self.battery_rate_max_discharge_scaled = 1 / 60.0
        self.reserve_max = 100.0
        self.now_utc = now_utc
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.count_register_writes = 0

    def get_current_charge_rate(self):
        return self.charge_rate

    def disable_charge_window(self):
        self.charge_time_enable = False

    def adjust_charge_window(self, charge_start_time, charge_end_time, minutes_now):
        self.charge_start_time_minutes = (charge_start_time - self.midnight_utc).total_seconds() / 60
        self.charge_end_time_minutes = (charge_end_time - self.midnight_utc).total_seconds() / 60
        self.charge_time_enable = True

    def adjust_charge_immediate(self, target_soc, freeze=False):
        self.immediate_charge_soc_target = target_soc
        self.immediate_charge_soc_freeze = freeze

    def adjust_export_immediate(self, target_soc, freeze=False):
        self.immediate_discharge_soc_target = target_soc
        self.immediate_discharge_soc_freeze = freeze

    def adjust_force_export(self, force_export, new_start_time=None, new_end_time=None):
        self.force_export = force_export
        if new_start_time is not None:
            delta = new_start_time - self.midnight_utc
            self.discharge_start_time_minutes = delta.total_seconds() / 60
        if new_end_time is not None:
            delta = new_end_time - self.midnight_utc
            self.discharge_end_time_minutes = delta.total_seconds() / 60

    def adjust_idle_time(self, charge_start=None, charge_end=None, discharge_start=None, discharge_end=None):
        self.idle_charge_start = charge_start
        self.idle_charge_end = charge_end
        self.idle_discharge_start = discharge_start
        self.idle_discharge_end = discharge_end

    def adjust_inverter_mode(self, force_export, changed_start_end=False):
        self.force_export = force_export
        self.changed_start_end = changed_start_end

    def adjust_reserve(self, reserve):
        self.reserve = reserve

    def adjust_pause_mode(self, pause_charge=False, pause_discharge=False):
        self.pause_charge = pause_charge
        self.pause_discharge = pause_discharge

    def adjust_battery_target(self, soc, isCharging=False, isExporting=False):
        self.soc_target = soc
        self.isCharging = isCharging
        self.isExporting = isExporting

    def adjust_charge_rate(self, charge_rate):
        self.charge_rate = charge_rate

    def adjust_discharge_rate(self, discharge_rate):
        self.discharge_rate = discharge_rate


def run_execute_test(
    my_predbat,
    name,
    charge_window_best=[],
    charge_limit_best=[],
    export_window_best=[],
    export_limits_best=[],
    car_slot=[],
    soc_kw=0,
    read_only=False,
    set_soc_enable=True,
    set_charge_window=False,
    set_export_window=False,
    set_charge_low_power=False,
    charge_low_power_margin=10,
    assert_charge_time_enable=False,
    assert_force_export=False,
    assert_pause_charge=False,
    assert_pause_discharge=False,
    assert_status="Demand",
    assert_charge_start_time_minutes=-1,
    assert_charge_end_time_minutes=-1,
    assert_discharge_start_time_minutes=-1,
    assert_discharge_end_time_minutes=-1,
    inverter_charge_time_minutes_start=-1,
    assert_charge_rate=None,
    assert_discharge_rate=None,
    assert_reserve=0,
    assert_soc_target=100,
    in_calibration=False,
    set_discharge_during_charge=True,
    assert_immediate_soc_target=None,
    set_reserve_enable=True,
    has_timed_pause=True,
    has_target_soc=True,
    has_charge_enable_time=True,
    inverter_hybrid=False,
    battery_max_rate=1000,
):
    print("Run scenario {}".format(name))
    my_predbat.soc_kw = soc_kw
    my_predbat.soc_max = 10.0
    my_predbat.reserve = 1
    my_predbat.soc_percent = calc_percent_limit(soc_kw, my_predbat.soc_max)
    my_predbat.set_read_only = read_only
    my_predbat.car_charging_slots = [car_slot]
    my_predbat.num_cars = 1
    my_predbat.inverter_hybrid = inverter_hybrid
    my_predbat.set_charge_low_power = set_charge_low_power
    my_predbat.charge_low_power_margin = charge_low_power_margin

    if assert_immediate_soc_target is None:
        assert_immediate_soc_target = assert_soc_target
    if assert_charge_rate is None:
        assert_charge_rate = battery_max_rate
    if assert_discharge_rate is None:
        assert_discharge_rate = battery_max_rate

    total_inverters = len(my_predbat.inverters)
    my_predbat.battery_rate_max_charge = battery_max_rate / 1000.0 * total_inverters / 60.0
    my_predbat.battery_rate_max_discharge = battery_max_rate / 1000.0 * total_inverters / 60.0
    for inverter in my_predbat.inverters:
        inverter.charge_start_time_minutes = inverter_charge_time_minutes_start
        inverter.soc_kw = soc_kw / total_inverters
        inverter.soc_max = my_predbat.soc_max / total_inverters
        inverter.soc_percent = calc_percent_limit(inverter.soc_kw, inverter.soc_max)
        inverter.in_calibration = in_calibration
        inverter.battery_rate_max_charge = my_predbat.battery_rate_max_charge / total_inverters
        inverter.battery_rate_max_discharge = my_predbat.battery_rate_max_discharge / total_inverters
        inverter.inv_has_timed_pause = has_timed_pause
        inverter.inv_has_target_soc = has_target_soc
        inverter.inv_has_charge_enable_time = has_charge_enable_time

    failed = False
    my_predbat.charge_window_best = charge_window_best
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.charge_limit_percent_best = [calc_percent_limit(x, my_predbat.soc_max) for x in charge_limit_best]
    my_predbat.export_window_best = export_window_best
    my_predbat.export_limits_best = export_limits_best
    my_predbat.set_charge_window = set_charge_window
    my_predbat.set_export_window = set_export_window
    my_predbat.set_soc_enable = set_soc_enable
    my_predbat.set_reserve_enable = set_reserve_enable
    my_predbat.set_reserve_hold = True
    my_predbat.set_export_freeze = True
    my_predbat.set_discharge_during_charge = set_discharge_during_charge
    my_predbat.car_charging_from_battery = False

    status, status_extra = my_predbat.execute_plan()

    for inverter in my_predbat.inverters:
        if assert_status != status:
            print("ERROR: Inverter {} status should be {} got {}".format(inverter.id, assert_status, status))
            failed = True
        if assert_charge_time_enable != inverter.charge_time_enable:
            print("ERROR: Inverter {} Charge time enable should be {} got {}".format(inverter.id, assert_charge_time_enable, inverter.charge_time_enable))
            failed = True
        if assert_force_export != inverter.force_export:
            print("ERROR: Inverter {} Force discharge should be {} got {}".format(inverter.id, assert_force_export, inverter.force_export))
            failed = True
        if assert_pause_charge != inverter.pause_charge:
            print("ERROR: Inverter {} Pause charge should be {} got {}".format(inverter.id, assert_pause_charge, inverter.pause_charge))
            failed = True
        if assert_pause_discharge != inverter.pause_discharge:
            print("ERROR: Inverter {} Pause discharge should be {} got {}".format(inverter.id, assert_pause_discharge, inverter.pause_discharge))
            failed = True
        if assert_charge_time_enable and assert_charge_start_time_minutes != inverter.charge_start_time_minutes:
            print("ERROR: Inverter {} Charge start time should be {} got {}".format(inverter.id, assert_charge_start_time_minutes, inverter.charge_start_time_minutes))
            failed = True
        if assert_charge_time_enable and assert_charge_end_time_minutes != inverter.charge_end_time_minutes:
            print("ERROR: Inverter {} Charge end time should be {} got {}".format(inverter.id, assert_charge_end_time_minutes, inverter.charge_end_time_minutes))
            failed = True
        if assert_force_export and assert_discharge_start_time_minutes != inverter.discharge_start_time_minutes:
            print("ERROR: Inverter {} Discharge start time should be {} got {}".format(inverter.id, assert_discharge_start_time_minutes, inverter.discharge_start_time_minutes))
            failed = True
        if assert_force_export and assert_discharge_end_time_minutes != inverter.discharge_end_time_minutes:
            print("ERROR: Inverter {} Discharge end time should be {} got {}".format(inverter.id, assert_discharge_end_time_minutes, inverter.discharge_end_time_minutes))
            failed = True
        if assert_charge_rate != inverter.charge_rate:
            print("ERROR: Inverter {} Charge rate should be {} got {}".format(inverter.id, assert_charge_rate, inverter.charge_rate))
            failed = True
        if assert_discharge_rate != inverter.discharge_rate:
            print("ERROR: Inverter {} Discharge rate should be {} got {}".format(inverter.id, assert_discharge_rate, inverter.discharge_rate))
            failed = True
        if assert_reserve != inverter.reserve:
            print("ERROR: Inverter {} Reserve should be {} got {}".format(inverter.id, assert_reserve, inverter.reserve))
            failed = True
        if assert_soc_target != inverter.soc_target:
            print("ERROR: Inverter {} SOC target should be {} got {}".format(inverter.id, assert_soc_target, inverter.soc_target))
            failed = True

        assert_soc_target_force = assert_immediate_soc_target if assert_status in ["Charging", "Hold charging", "Freeze charging", "Hold charging, Hold for iBoost", "Freeze charging, Hold for iBoost"] else 0
        if not set_charge_window:
            assert_soc_target_force = -1
        if inverter.immediate_charge_soc_target != assert_soc_target_force:
            print("ERROR: Inverter {} Immediate charge SOC target should be {} got {}".format(inverter.id, assert_soc_target_force, inverter.immediate_charge_soc_target))
            failed = True
        if assert_status in ["Hold charging"] and inverter.immediate_charge_soc_freeze != True:
            print("ERROR: Inverter {} Immediate charge SOC freeze should be True got {}".format(inverter.id, inverter.immediate_charge_soc_freeze))
            failed = True
        assert_soc_target_force_dis = assert_immediate_soc_target if assert_status in ["Exporting", "Freeze exporting"] else 0
        if not set_export_window:
            assert_soc_target_force_dis = -1
        if inverter.immediate_discharge_soc_target != assert_soc_target_force_dis:
            print("ERROR: Inverter {} Immediate export SOC target should be {} got {}".format(inverter.id, assert_soc_target_force_dis, inverter.immediate_discharge_soc_target))
            failed = True
        if assert_status in ["Freeze exporting"] and inverter.immediate_discharge_soc_freeze != True:
            print("ERROR: Inverter {} Immediate export SOC freeze should be True got {}".format(inverter.id, inverter.immediate_discharge_soc_freeze))
            failed = True

    return failed


def run_single_debug(my_predbat, debug_file):
    print("**** Running debug test {} ****\n".format(debug_file))

    reset_inverter(my_predbat)
    my_predbat.read_debug_yaml(debug_file)
    my_predbat.config_root = "./"
    my_predbat.save_restore_dir = "./"
    my_predbat.fetch_config_options()

    end_record = my_predbat.end_record
    print("minutes_now {}".format(my_predbat.minutes_now))
    failed = False

    pv_step = my_predbat.pv_forecast_minute_step
    pv10_step = my_predbat.pv_forecast_minute10_step
    load_step = my_predbat.load_minutes_step
    load10_step = my_predbat.load_minutes_step10

    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.debug_enable = True

    charge_limit_best = my_predbat.charge_limit_best
    charge_window_best = my_predbat.charge_window_best
    export_window_best = my_predbat.export_window_best
    export_limits_best = my_predbat.export_limits_best

    failed = False
    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        charge_limit_best, charge_window_best, export_window_best, export_limits_best, False, end_record=end_record, save="best"
    )
    # Save plan
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.charge_limit_percent_best = calc_percent_limit(charge_limit_best, my_predbat.soc_max)
    my_predbat.export_limits_best = export_limits_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best

    # Pre-optimise all plan
    my_predbat.publish_html_plan(pv_step, pv10_step, load_step, load10_step, end_record)
    open("plan_levels.html", "w").write(my_predbat.html_plan)

    # Optimise windows
    best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import = my_predbat.optimise_all_windows(metric, metric_keep)
    charge_limit_best = my_predbat.charge_limit_best
    export_limits_best = my_predbat.export_limits_best
    charge_window_best = my_predbat.charge_window_best
    export_window_best = my_predbat.export_window_best

    # Predict
    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        charge_limit_best, charge_window_best, export_window_best, export_limits_best, False, end_record=end_record, save="best"
    )

    # Save plan
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.charge_limit_percent_best = calc_percent_limit(charge_limit_best, my_predbat.soc_max)
    my_predbat.export_limits_best = export_limits_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best

    my_predbat.publish_html_plan(pv_step, pv10_step, load_step, load10_step, end_record)
    open("plan.html", "w").write(my_predbat.html_plan)
    print("Wrote plan to plan.html")


def run_execute_tests(my_predbat):
    print("**** Running execute tests ****\n")
    reset_inverter(my_predbat)

    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best2 = [{"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best3 = [{"start": my_predbat.minutes_now - 30, "end": my_predbat.minutes_now, "average": 1}, {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best4 = [{"start": my_predbat.minutes_now + 24 * 60, "end": my_predbat.minutes_now + 60 + 24 * 60, "average": 1}]
    charge_window_best5 = [{"start": my_predbat.minutes_now - 24 * 60, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best6 = [{"start": my_predbat.minutes_now + 8 * 60, "end": my_predbat.minutes_now + 60 + 8 * 60, "average": 1}]
    charge_window_best7 = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 23 * 60, "average": 1}]
    charge_window_best8 = [{"start": 0, "end": my_predbat.minutes_now + 12 * 60, "average": 1}]
    charge_window_best9 = [{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 90, "average": 1}]
    charge_window_best_short = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 15, "average": 1}]
    charge_limit_best = [10, 10]
    charge_limit_best2 = [5]
    charge_limit_best_frz = [1]
    export_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 1}]
    export_window_best2 = [{"start": my_predbat.minutes_now - 30, "end": my_predbat.minutes_now + 90, "average": 1}]
    export_window_best3 = [{"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 90, "average": 1}]
    export_window_best4 = [{"start": my_predbat.minutes_now + 15, "end": my_predbat.minutes_now + 90, "average": 1}]
    export_window_best5 = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 23 * 60, "average": 1}]
    export_window_best6 = [{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 90, "average": 1}]
    export_window_best7 = [{"start": 0, "end": my_predbat.minutes_now + 12 * 60, "average": 1}]
    export_limits_best = [0]
    export_limits_best2 = [50]
    export_limits_best_frz = [99]

    inverters = [ActiveTestInverter(0, 0, 10.0, my_predbat.now_utc), ActiveTestInverter(1, 0, 10.0, my_predbat.now_utc)]
    my_predbat.inverters = inverters

    failed = False
    failed |= run_execute_test(my_predbat, "off")
    my_predbat.holiday_days_left = 2
    failed |= run_execute_test(my_predbat, "off_holiday", assert_status="Demand (Holiday)")
    my_predbat.holiday_days_left = 0

    failed |= run_execute_test(my_predbat, "no_charge", charge_window_best=charge_window_best, charge_limit_best=charge_limit_best)
    failed |= run_execute_test(my_predbat, "no_charge2", set_charge_window=True, set_export_window=True, set_discharge_during_charge=False)
    failed |= run_execute_test(my_predbat, "no_charge_future", set_charge_window=True, set_export_window=True, charge_window_best=charge_window_best4, charge_limit_best=charge_limit_best)
    failed |= run_execute_test(my_predbat, "no_charge_future_hybrid", set_charge_window=True, set_export_window=True, charge_window_best=charge_window_best4, charge_limit_best=charge_limit_best, inverter_hybrid=True)
    failed |= run_execute_test(
        my_predbat,
        "no_charge_future_no_soc",
        set_charge_window=True,
        set_export_window=True,
        charge_window_best=charge_window_best4,
        charge_limit_best=charge_limit_best,
        has_target_soc=False,
        assert_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_charge_future_no_enable_time",
        set_charge_window=True,
        set_export_window=True,
        charge_window_best=charge_window_best4,
        charge_limit_best=charge_limit_best,
        has_target_soc=True,
        has_charge_enable_time=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_charge_future_no_enable_time_hybrid",
        set_charge_window=True,
        set_export_window=True,
        charge_window_best=charge_window_best4,
        charge_limit_best=charge_limit_best,
        has_target_soc=True,
        has_charge_enable_time=False,
        inverter_hybrid=True,
        assert_soc_target=0,
    )
    if failed:
        return failed

    # Iboost hold tests
    my_predbat.iboost_enable = True
    my_predbat.iboost_prevent_discharge = True
    my_predbat.iboost_running_full = True
    failed |= run_execute_test(my_predbat, "no_charge_iboost", set_charge_window=True, set_export_window=True, assert_pause_discharge=True, assert_status="Hold for iBoost")

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_iboost",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_pause_discharge=True,
        assert_status="Freeze charging, Hold for iBoost",
        assert_discharge_rate=1000,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_iboost2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_pause_discharge=False,
        assert_status="Freeze charging, Hold for iBoost",
        assert_discharge_rate=0,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
        assert_reserve=100,
        has_timed_pause=False,
    )
    if failed:
        return failed

    my_predbat.iboost_prevent_discharge = False
    failed |= run_execute_test(my_predbat, "no_charge_iboost2", set_charge_window=True, set_export_window=True)
    my_predbat.iboost_running_full = False
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_low_power1",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        battery_max_rate=2000,
    )
    if failed:
        return failed

    # 60 minutes - 10 minute margin = 50 minutes to add 0.5kWh to each battery (x2 inverters)
    # (60 / 50) * 500 = 600
    failed |= run_execute_test(
        my_predbat,
        "charge_low_power2a",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=9,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=600,
        battery_max_rate=2000,
    )
    if failed:
        return failed

    # 60 minutes - 10 minute margin = 50 minutes to add 0.4kWh to each battery (x2 inverters)
    # (60 / 50) * 400 = 480
    failed |= run_execute_test(
        my_predbat,
        "charge_low_power2b",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=9.2,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=600,  # Within 10%
        battery_max_rate=2000,
    )
    if failed:
        return failed

    # 60 minutes - 30 minute margin = 30 minutes to add 0.4kWh to each battery (x2 inverters)
    # (60 / 20) * 400 = 1200
    failed |= run_execute_test(
        my_predbat,
        "charge_low_power2c",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=9.2,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        charge_low_power_margin=40,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=1200,
        battery_max_rate=2000,
    )
    if failed:
        return failed

    # 60 minutes - 10 minute margin = 50 minutes to add 0.45kWh to each battery (x2 inverters)
    # (60 / 50) * 450 = 540
    failed |= run_execute_test(
        my_predbat,
        "charge_low_power2d",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=9.1,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=600,
        battery_max_rate=2000,
    )
    if failed:
        return failed

    my_predbat.battery_charge_power_curve = {
        100: 0.50,
        99: 0.50,
        98: 0.50,
        97: 0.50,
        96: 0.50,
        95: 0.50,
        94: 1.00,
        93: 1.00,
        92: 1.00,
        91: 1.00,
        90: 1.00,
        89: 1.00,
        88: 1.00,
        87: 1.00,
        86: 1.00,
        85: 1.00,
    }

    # 60 minutes - 10 minute margin = 50 minutes to add 0.75kWh to each battery (x2 inverters)
    # (60 / 50) * 750 = 900
    # But with the low power curve it will go at half rate from 95%

    failed |= run_execute_test(
        my_predbat,
        "charge_low_power3a",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=8.0,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_rate=1300,
        battery_max_rate=2000,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_low_power_short",
        charge_window_best=charge_window_best_short,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        soc_kw=9.835,
        set_charge_window=True,
        set_export_window=True,
        set_charge_low_power=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 15,
        assert_charge_rate=2000,
        battery_max_rate=2000,
    )
    if failed:
        return failed

    # Reset curve
    my_predbat.battery_charge_power_curve = {}

    failed |= run_execute_test(
        my_predbat,
        "charge_long",
        charge_window_best=charge_window_best8,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=my_predbat.minutes_now,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 12 * 60,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_no_soc",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_target_soc=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_no_enable_time",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_charge_enable_time=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_no_enable_time_no_soc",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_target_soc=False,
        has_charge_enable_time=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge2a",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        set_discharge_during_charge=False,
        assert_discharge_rate=1000,
        assert_pause_discharge=True,
        assert_soc_target=50,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge2b",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=5,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=1000,
        assert_pause_discharge=True,
        assert_reserve=0,
        assert_immediate_soc_target=50,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge2c",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=5,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=0,
        assert_pause_discharge=False,
        assert_reserve=51,
        assert_immediate_soc_target=50,
        has_timed_pause=False,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge23",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_soc_target=50,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge3",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        soc_kw=4,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        set_discharge_during_charge=False,
        assert_discharge_rate=1000,
        assert_pause_discharge=True,
        assert_soc_target=50,
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge4_no_reserve",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=5,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=1000,
        assert_pause_discharge=True,
        assert_immediate_soc_target=50,
        set_reserve_enable=False,
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge4_no_reserve_no_pause",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=5,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=0,
        assert_pause_discharge=False,
        assert_immediate_soc_target=50,
        assert_soc_target=50,
        set_reserve_enable=False,
        has_timed_pause=False,
        assert_charge_time_enable=True,
    )
    if failed:
        return failed

    # Charge/discharge with rate
    for inverter in my_predbat.inverters:
        inverter.inv_charge_discharge_with_rate = True
    failed |= run_execute_test(
        my_predbat,
        "charge_with_rate",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=0,
    )
    for inverter in my_predbat.inverters:
        inverter.inv_charge_discharge_with_rate = False
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_midnight1",
        charge_window_best=charge_window_best7,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=my_predbat.minutes_now,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 23 * 60,
    )
    # Can span midnight false test
    for inverter in my_predbat.inverters:
        inverter.inv_can_span_midnight = False
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_midnight2",
        charge_window_best=charge_window_best7,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=my_predbat.minutes_now,
        assert_charge_end_time_minutes=24 * 60 - 1,
    )
    for inverter in my_predbat.inverters:
        inverter.inv_can_span_midnight = True
    if failed:
        return failed

    my_predbat.debug_enable = True
    failed |= run_execute_test(
        my_predbat,
        "charge_shift",
        charge_window_best=charge_window_best3,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge_shift2",
        charge_window_best=charge_window_best5,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge_shift3",
        charge_window_best=charge_window_best5,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        inverter_charge_time_minutes_start=-24 * 60,
        assert_charge_start_time_minutes=my_predbat.minutes_now,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    my_predbat.debug_enable = False

    # Reset inverters
    inverters = [ActiveTestInverter(0, 0, 10.0, my_predbat.now_utc), ActiveTestInverter(1, 0, 10.0, my_predbat.now_utc)]
    my_predbat.inverters = inverters

    failed |= run_execute_test(my_predbat, "calibration", in_calibration=True, assert_status="Calibration", assert_charge_time_enable=False, assert_reserve=0, assert_soc_target=100)
    failed |= run_execute_test(my_predbat, "no_charge3", set_charge_window=True, set_export_window=True)
    failed |= run_execute_test(my_predbat, "charge_read_only", charge_window_best=charge_window_best, charge_limit_best=charge_limit_best, set_charge_window=True, set_export_window=True, read_only=True, assert_status="Read-Only")
    failed |= run_execute_test(
        my_predbat,
        "charge3",
        inverter_charge_time_minutes_start=1,
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge4",
        inverter_charge_time_minutes_start=24 * 60 - 1,
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=my_predbat.minutes_now,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge5",
        inverter_charge_time_minutes_start=24 * 60 - 1,
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=my_predbat.minutes_now,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        set_reserve_enable=False,
        has_timed_pause=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_hold",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Hold charging",
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge_hold2a",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,
        assert_pause_discharge=True,
        assert_status="Hold charging",
        assert_discharge_rate=1000,
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=50,
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge_hold2b",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,
        assert_pause_discharge=False,
        assert_status="Hold charging",
        assert_discharge_rate=0,
        assert_reserve=51,
        assert_soc_target=100,
        assert_immediate_soc_target=50,
        has_timed_pause=False,
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze1a",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_pause_discharge=True,
        assert_status="Freeze charging",
        assert_discharge_rate=1000,
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
    )
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze1b",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_pause_discharge=False,
        assert_status="Freeze charging",
        assert_discharge_rate=0,
        assert_reserve=100,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
        has_timed_pause=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_no_pause",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_pause_discharge=False,
        assert_status="Freeze charging",
        assert_discharge_rate=0,
        assert_reserve=100,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
        set_reserve_enable=False,
        has_timed_pause=False,
        assert_charge_time_enable=True,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,
        assert_pause_discharge=True,
        assert_status="Freeze charging",
        assert_discharge_rate=1000,
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=50,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze2_no_pause",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=5,
        assert_pause_discharge=False,
        assert_status="Freeze charging",
        assert_discharge_rate=0,
        assert_reserve=0,
        assert_soc_target=50,
        assert_immediate_soc_target=50,
        set_reserve_enable=False,
        has_timed_pause=False,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    if failed:
        return failed

    failed |= run_execute_test(my_predbat, "charge_freeze3", charge_window_best=charge_window_best2, charge_limit_best=charge_limit_best_frz, assert_charge_time_enable=False, set_charge_window=True, set_export_window=True, soc_kw=5)

    failed |= run_execute_test(my_predbat, "no_charge4", set_charge_window=True, set_export_window=True)
    failed |= run_execute_test(
        my_predbat,
        "charge_later",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_later_hybrid",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        inverter_hybrid=True,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_later_no_soc",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_target_soc=False,
        assert_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "freeze_later_no_soc",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_target_soc=False,
        assert_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_later_no_enable_time",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_charge_enable_time=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_later_no_enable_time_hybrid",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_charge_enable_time=False,
        inverter_hybrid=True,
        assert_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_later_no_enable_time_no_soc",
        charge_window_best=charge_window_best2,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_charge_start_time_minutes=my_predbat.minutes_now + 30,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        has_charge_enable_time=False,
        has_target_soc=False,
        assert_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(my_predbat, "charge_later2", charge_window_best=charge_window_best6, charge_limit_best=charge_limit_best, assert_charge_time_enable=False, set_charge_window=True, set_export_window=True, assert_status="Demand")
    failed |= run_execute_test(my_predbat, "no_charge5", set_charge_window=True, set_export_window=True, assert_immediate_soc_target=0)
    # Reset inverters
    inverters = [ActiveTestInverter(0, 0, 10.0, my_predbat.now_utc), ActiveTestInverter(1, 0, 10.0, my_predbat.now_utc)]
    my_predbat.inverters = inverters

    failed |= run_execute_test(my_predbat, "no_discharge", export_window_best=export_window_best, export_limits_best=export_limits_best)
    if failed:
        return failed
    failed |= run_execute_test(my_predbat, "no_discharge2", export_window_best=export_window_best, export_limits_best=export_limits_best, set_charge_window=True, set_export_window=True, soc_kw=0, assert_status="Hold exporting")
    if failed:
        return failed
    failed |= run_execute_test(my_predbat, "no_discharge3a", export_window_best=export_window_best3, export_limits_best=export_limits_best, set_charge_window=True, set_export_window=True, soc_kw=0)
    if failed:
        return failed
    failed |= run_execute_test(my_predbat, "no_discharge3b", export_window_best=export_window_best6, export_limits_best=export_limits_best, set_charge_window=True, set_export_window=True, soc_kw=0)
    if failed:
        return failed
    failed |= run_execute_test(my_predbat, "no_discharge4", export_window_best=export_window_best4, export_limits_best=export_limits_best, set_charge_window=True, set_export_window=True, soc_kw=0)
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_discharge_car",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=0,
        assert_status="Hold exporting, Hold for car",
        car_slot=charge_window_best,
        assert_pause_discharge=True,
        assert_discharge_rate=1000,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_discharge_car2",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=0,
        assert_status="Hold exporting, Hold for car",
        car_slot=charge_window_best,
        assert_pause_discharge=False,
        assert_discharge_rate=0,
        has_timed_pause=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge_long",
        export_window_best=export_window_best7,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 12 * 60 + 1,
        soc_kw=10,
        assert_status="Exporting",
        assert_force_export=True,
        assert_immediate_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge_charge",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        charge_limit_best=charge_limit_best,
        charge_window_best=charge_window_best9,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge2_no_reserve",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best2,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=50,
        assert_soc_target=50,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        set_reserve_enable=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge2_no_reserve_no_pause",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best2,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=50,
        assert_soc_target=50,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        set_reserve_enable=False,
        has_timed_pause=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge2",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best2,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=50,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge3",
        export_window_best=export_window_best2,
        export_limits_best=export_limits_best,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 90 + 1,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge_midnight1",
        export_window_best=export_window_best5,
        export_limits_best=export_limits_best,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 23 * 60 + 1,
    )
    if failed:
        return failed

    # Can span midnight false test
    for inverter in my_predbat.inverters:
        inverter.inv_can_span_midnight = False

    failed |= run_execute_test(
        my_predbat,
        "discharge_midnight2",
        export_window_best=export_window_best5,
        export_limits_best=export_limits_best,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=24 * 60 - 1,
    )
    for inverter in my_predbat.inverters:
        inverter.inv_can_span_midnight = True

    # Charge/discharge with rate
    for inverter in my_predbat.inverters:
        inverter.inv_charge_discharge_with_rate = True
    failed |= run_execute_test(
        my_predbat,
        "discharge_with_rate",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_charge_rate=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge_freeze",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best_frz,
        assert_force_export=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=9,
        assert_status="Freeze exporting",
        assert_pause_charge=True,
        assert_charge_rate=0,
        assert_immediate_soc_target=90,
    )
    for inverter in my_predbat.inverters:
        inverter.inv_charge_discharge_with_rate = False
    if failed:
        return failed
    failed |= run_execute_test(
        my_predbat,
        "discharge_freeze2",
        export_window_best=export_window_best2,
        export_limits_best=export_limits_best_frz,
        assert_force_export=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=9,
        assert_status="Freeze exporting",
        assert_pause_charge=True,
        assert_charge_rate=1000,
        assert_immediate_soc_target=90,
    )
    failed |= run_execute_test(
        my_predbat,
        "discharge_freeze2b",
        export_window_best=export_window_best2,
        export_limits_best=export_limits_best_frz,
        assert_force_export=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=9,
        assert_status="Freeze exporting",
        assert_pause_charge=False,
        assert_charge_rate=0,
        assert_immediate_soc_target=90,
        has_timed_pause=False,
    )
    failed |= run_execute_test(my_predbat, "no_charge5", set_charge_window=True, set_export_window=True)
    failed |= run_execute_test(my_predbat, "car", car_slot=charge_window_best, set_charge_window=True, set_export_window=True, assert_status="Hold for car", assert_pause_discharge=True, assert_discharge_rate=1000)
    failed |= run_execute_test(my_predbat, "car2", car_slot=charge_window_best, set_charge_window=True, set_export_window=True, assert_status="Hold for car", assert_pause_discharge=False, assert_discharge_rate=0, has_timed_pause=False)
    failed |= run_execute_test(
        my_predbat,
        "car_charge",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        soc_kw=0,
        car_slot=charge_window_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    failed |= run_execute_test(
        my_predbat,
        "car_discharge",
        car_slot=charge_window_best,
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
    )

    # Reset test
    my_predbat.reset_inverter()
    failed |= run_execute_test(
        my_predbat,
        "demand_after_reset",
        set_charge_window=True,
        set_export_window=True,
        assert_status="Demand",
        assert_reserve=0,
        assert_immediate_soc_target=0,
        assert_soc_target=100,
    )

    return failed


def run_inverter_multi_tests(my_predbat):
    print("**** Running inverter multi tests ****\n")

    failed = False
    inverter = DummyInverter(my_predbat.log)

    failed |= run_inverter_multi_test("charge", my_predbat, inverter, 50, isCharging=False, assert_soc=50)
    failed |= run_inverter_multi_test("charge_soc", my_predbat, inverter, 50, isCharging=False, assert_soc=50, soc_kw=50.0, soc_kw_all=50.0)
    failed |= run_inverter_multi_test("charge2", my_predbat, inverter, 50, isCharging=False, assert_soc=50, battery_rate_max_charge_all=2.0, soc_max_all=200.0)
    failed |= run_inverter_multi_test("charge3", my_predbat, inverter, 50, isCharging=False, assert_soc=75, battery_rate_max_charge_all=2.0, soc_max=50.0, soc_max_all=150.0)
    failed |= run_inverter_multi_test("charge4", my_predbat, inverter, 50, isCharging=True, assert_soc=67, battery_rate_max_charge=2.0, battery_rate_max_charge_all=3.0, soc_max_all=200.0)
    failed |= run_inverter_multi_test("charge5", my_predbat, inverter, 50, isCharging=True, assert_soc=33, battery_rate_max_charge=1.0, battery_rate_max_charge_all=3.0, soc_max_all=200.0)
    failed |= run_inverter_multi_test("discharge", my_predbat, inverter, 50, isCharging=False, assert_soc=50, soc_max_all=200.0, soc_max=100.0, soc_kw=100.0, soc_kw_all=200.0, battery_rate_max_charge_all=2.0)
    return failed


def run_window_sort_test(name, my_predbat, charge_window_best, export_window_best, expected=[], inverter_loss=1.0, metric_battery_cycle=0.0, battery_loss=1.0, battery_loss_discharge=1.0):
    failed = False
    end_record = my_predbat.forecast_minutes
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.inverter_loss = inverter_loss
    my_predbat.metric_battery_cycle = metric_battery_cycle
    my_predbat.battery_loss = battery_loss
    my_predbat.battery_loss_discharge = battery_loss_discharge

    print("Starting window sort test {}".format(name))

    record_charge_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, charge_window_best), 1)
    record_export_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, export_window_best), 1)

    window_sorted, window_index, price_set, price_links = my_predbat.sort_window_by_price_combined(charge_window_best[:record_charge_windows], export_window_best[:record_export_windows])

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
        print("Inputs: {} {}".format(charge_window_best, export_window_best))
        print("Results: {}".format(results))

    return failed


def run_intersect_window_tests(my_predbat):
    print("**** Running intersect window tests ****")
    failed = False
    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 10}]
    export_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 10}]
    charge_limit_best = [4]
    export_limit_best = [2]
    new_limit_best, new_window_best = remove_intersecting_windows(charge_limit_best, charge_window_best, export_limit_best, export_window_best)
    if len(new_window_best) != 0:
        print("ERROR: Expected no windows but got {}".format(new_window_best))
        failed = True
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

    export_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": export_rate}]
    failed |= run_window_sort_test("single_discharge", my_predbat, [], export_window_best, expected=["d_0_5.0"])
    failed |= run_window_sort_test("single_charge_discharge", my_predbat, charge_window_best, export_window_best, expected=["c_0_10.0", "d_0_5.0"])
    failed |= run_window_sort_test("single_charge_discharge_loss", my_predbat, charge_window_best, export_window_best, expected=["c_0_20.0", "d_0_2.5"], inverter_loss=0.5)
    export_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 50.0}]
    failed |= run_window_sort_test("single_charge_discharge_loss2", my_predbat, charge_window_best, export_window_best, expected=["d_0_25.0", "c_0_20.0"], inverter_loss=0.5)
    failed |= run_window_sort_test("single_charge_discharge_loss3", my_predbat, charge_window_best, export_window_best, expected=["c_0_200.0", "d_0_25.0"], inverter_loss=0.5, battery_loss=0.1)
    failed |= run_window_sort_test(
        "single_charge_discharge_loss4",
        my_predbat,
        charge_window_best,
        export_window_best,
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
        export_window_best,
        expected=["c_1_400.0", "c_0_200.0", "d_0_2.5"],
        inverter_loss=0.5,
        battery_loss=0.1,
        battery_loss_discharge=0.1,
    )
    export_window_best.append({"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": export_rate * 3})
    failed |= run_window_sort_test("single_charge_discharge3", my_predbat, charge_window_best, export_window_best, expected=["d_0_50.0", "c_1_20.0", "d_1_15.0", "c_0_10.0"])
    failed |= run_window_sort_test("single_charge_discharge3_c1", my_predbat, charge_window_best, export_window_best, expected=["d_0_49.0", "c_1_21.0", "d_1_14.0", "c_0_11.0"], metric_battery_cycle=1.0)

    return failed


def run_optimise_all_windows(
    name,
    my_predbat,
    charge_window_best=[],
    export_window_best=[],
    pv_amount=0,
    load_amount=0,
    expect_charge_limit=[],
    expect_export_limit=[],
    expect_best_price=0.0,
    rate_import=10.0,
    rate_export=5.0,
    battery_size=100.0,
    battery_soc=0.0,
    hybrid=False,
    inverter_loss=1.0,
    best_soc_keep=0.0,
):
    print("Starting optimise all windows test {}".format(name))
    end_record = my_predbat.forecast_minutes
    failed = False
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.inverter_hybrid = hybrid
    my_predbat.inverter_loss = inverter_loss
    my_predbat.best_soc_keep = best_soc_keep

    reset_rates(my_predbat, rate_import, rate_export)
    update_rates_import(my_predbat, charge_window_best)
    update_rates_export(my_predbat, export_window_best)

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = pv_amount / (60 / 5)
        load_step[minute] = load_amount / (60 / 5)
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.debug_enable = True

    charge_limit_best = [0 for n in range(len(charge_window_best))]
    export_limits_best = [100 for n in range(len(export_window_best))]

    failed = False
    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        charge_limit_best, charge_window_best, export_window_best, export_limits_best, False, end_record=end_record
    )
    # Save plan
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.charge_limit_percent_best = calc_percent_limit(charge_limit_best, my_predbat.soc_max)
    my_predbat.export_limits_best = export_limits_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best

    # Optimise windows
    best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import = my_predbat.optimise_all_windows(metric, metric_keep)
    charge_limit_best = my_predbat.charge_limit_best
    export_limits_best = my_predbat.export_limits_best
    charge_window_best = my_predbat.charge_window_best
    export_window_best = my_predbat.export_window_best

    # Predict
    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        charge_limit_best, charge_window_best, export_window_best, export_limits_best, False, end_record=end_record, save="best"
    )

    # Save plan
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.charge_limit_percent_best = calc_percent_limit(charge_limit_best, my_predbat.soc_max)
    my_predbat.export_limits_best = export_limits_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best

    if len(expect_charge_limit) != len(my_predbat.charge_limit_best):
        print("ERROR: Expected {} charge limits but got {}".format(len(expect_charge_limit), len(my_predbat.charge_limit_best)))
        failed = True
    else:
        for n in range(len(expect_charge_limit)):
            if expect_charge_limit[n] != my_predbat.charge_limit_best[n]:
                print("ERROR: Expected charge limit {} is {} but got {}".format(n, expect_charge_limit[n], my_predbat.charge_limit_best[n]))
                failed = True
    if len(expect_export_limit) != len(my_predbat.export_limits_best):
        print("ERROR: Expected {} discharge limits but got {}".format(len(expect_export_limit), len(my_predbat.export_limits_best)))
        failed = True
    else:
        for n in range(len(expect_export_limit)):
            if expect_export_limit[n] != my_predbat.export_limits_best[n]:
                print("ERROR: Expected discharge limit {} is {} but got {}".format(n, expect_export_limit[n], my_predbat.export_limits_best[n]))
                failed = True

    if failed:
        my_predbat.publish_html_plan(my_predbat.pv_forecast_minute_step, my_predbat.pv_forecast_minute_step, my_predbat.load_minutes_step, my_predbat.load_minutes_step, end_record)
        open("plan.html", "w").write(my_predbat.html_plan)
        print("Wrote plan to plan.html")

    return failed


def run_optimise_all_windows_tests(my_predbat):
    print("**** Running Optimise all windows tests ****")
    reset_inverter(my_predbat)
    failed = False

    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}]

    failed |= run_optimise_all_windows(
        "single",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=[0],
        load_amount=1.0,
        pv_amount=0,
        expect_best_price=10.0 / 0.9,
        inverter_loss=0.9,
    )

    # Created2
    charge_window_best = []
    export_window_best = []
    expect_charge_limit = []
    expect_export_limit = []
    for n in range(0, 48):
        price = 16 - n % 16
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})
        expect_charge_limit.append(100 if price <= 5.0 else 0)
    failed |= run_optimise_all_windows(
        "created2",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=5 / 0.9,
        inverter_loss=0.9,
        best_soc_keep=0.5,
    )
    if failed:
        return failed

    charge_window_best = []
    export_window_best = []
    expect_charge_limit = []
    expect_export_limit = []
    for n in range(0, 48):
        n_mod = n % 24
        off_peak = True if n_mod > 12 else False
        price = 7 if off_peak else 30
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})
        expect_charge_limit.append(10 if off_peak else 0)
    failed |= run_optimise_all_windows(
        "off_peak",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.5,
        pv_amount=0,
        expect_best_price=5 / 0.9,
        inverter_loss=0.9,
        best_soc_keep=0.5,
        battery_size=10,
        battery_soc=5,
        rate_export=15,
    )

    return failed


def run_optimise_levels_tests(my_predbat):
    print("**** Running Optimise levels tests ****")
    reset_inverter(my_predbat)
    failed = False

    # Single charge window
    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}]

    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "single",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=[0],
        load_amount=1.0,
        pv_amount=0,
        expect_best_price=10.0 / 0.9,
        inverter_loss=0.9,
    )
    failed |= this_failed
    if failed:
        return failed

    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "single_pv",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=[0],
        load_amount=1.0,
        pv_amount=1.0,
        expect_best_price=10.0 / 0.9,
        inverter_loss=0.9,
    )
    failed |= this_failed
    if failed:
        return failed

    # Two windows charge low + high rate
    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}, {"start": my_predbat.minutes_now + 120, "end": my_predbat.minutes_now + 240, "average": 6}]

    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "dual",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=[0, 100],
        load_amount=0.5,
        pv_amount=0,
        expect_best_price=6.0 / 0.9,
        inverter_loss=0.9,
    )
    failed |= this_failed
    if failed:
        return failed

    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "dual_pv", my_predbat, charge_window_best=charge_window_best, expect_charge_limit=[0, 0], load_amount=1.0, pv_amount=1.0, expect_best_price=6.0 / 0.9, inverter_loss=0.9
    )
    failed |= this_failed
    if failed:
        return failed

    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "dual_pv2", my_predbat, charge_window_best=charge_window_best, expect_charge_limit=[0, 100], load_amount=2.0, pv_amount=1.0, expect_best_price=6.0 / 0.9, inverter_loss=0.9
    )
    failed |= this_failed
    if failed:
        return failed

    # Discharge
    export_window_best = [{"start": my_predbat.minutes_now + 240, "end": my_predbat.minutes_now + 300, "average": 7.5}]
    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "discharge",
        my_predbat,
        charge_window_best=charge_window_best,
        export_window_best=export_window_best,
        expect_charge_limit=[0, 100],
        expect_export_limit=[0],
        load_amount=0,
        pv_amount=0,
        expect_best_price=6.0,
        inverter_loss=1,
    )
    failed |= this_failed
    if failed:
        return failed

    # Discharge
    export_window_best = [{"start": my_predbat.minutes_now + 240, "end": my_predbat.minutes_now + 300, "average": 6}]
    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "discharge2",
        my_predbat,
        charge_window_best=charge_window_best,
        export_window_best=export_window_best,
        expect_charge_limit=[0, 100],
        expect_export_limit=[100],
        load_amount=0,
        pv_amount=0,
        expect_best_price=6.0,
        inverter_loss=1,
        rate_export=6.0,
    )
    failed |= this_failed
    if failed:
        return failed

    # Created
    charge_window_best = []
    export_window_best = []
    expect_charge_limit = []
    expect_export_limit = []
    for n in range(0, 48):
        price = n % 8
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})
        expect_charge_limit.append(100 if (price * 0.9) <= 5.0 else 0)
    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "created1",
        my_predbat,
        charge_window_best=charge_window_best,
        export_window_best=export_window_best,
        expect_charge_limit=expect_charge_limit,
        expect_export_limit=expect_export_limit,
        load_amount=0.5,
        pv_amount=0,
        expect_best_price=5.0 / 0.9,
        inverter_loss=0.9,
    )
    failed |= this_failed
    if failed:
        return failed

    # Created2
    charge_window_best = []
    export_window_best = []
    expect_charge_limit = []
    expect_export_limit = []
    for n in range(0, 48):
        price = 16 - n % 16
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})
        expect_charge_limit.append(100 if (price * 0.9) <= 5.0 else 0)
    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "created2",
        my_predbat,
        charge_window_best=charge_window_best,
        export_window_best=export_window_best,
        expect_charge_limit=expect_charge_limit,
        expect_export_limit=expect_export_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=5.0 / 0.9,
        inverter_loss=0.9,
        best_soc_keep=0.5,
    )
    failed |= this_failed
    if failed:
        return failed

    return failed


def run_optimise_levels(
    name,
    my_predbat,
    charge_window_best=[],
    export_window_best=[],
    pv_amount=0,
    load_amount=0,
    expect_charge_limit=[],
    expect_export_limit=[],
    expect_best_price=0.0,
    rate_import=10.0,
    rate_export=5.0,
    battery_size=100.0,
    battery_soc=0.0,
    hybrid=False,
    inverter_loss=1.0,
    best_soc_keep=0.0,
):
    end_record = my_predbat.forecast_minutes
    failed = False
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.inverter_hybrid = hybrid
    my_predbat.inverter_loss = inverter_loss
    my_predbat.best_soc_keep = best_soc_keep

    reset_rates(my_predbat, rate_import, rate_export)
    update_rates_import(my_predbat, charge_window_best)
    update_rates_export(my_predbat, export_window_best)

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = pv_amount / (60 / 5)
        load_step[minute] = load_amount / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.debug_enable = True

    charge_limit_best = [0 for n in range(len(charge_window_best))]
    export_limits_best = [100 for n in range(len(export_window_best))]

    record_charge_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, charge_window_best), 1)
    record_export_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, export_window_best), 1)
    print("Starting optimise levels test {}".format(name))

    window_sorted, window_index, price_set, price_links = my_predbat.sort_window_by_price_combined(charge_window_best[:record_charge_windows], export_window_best[:record_export_windows])

    my_predbat.optimise_charge_windows_reset(reset_all=True)
    my_predbat.optimise_charge_windows_manual()
    (
        charge_limit_best,
        export_limits_best,
        best_price,
        best_price_export,
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
        record_export_windows,
        charge_limit_best,
        charge_window_best,
        export_window_best,
        export_limits_best,
        end_record=end_record,
        fast=True,
        quiet=True,
    )

    # Predict
    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        charge_limit_best, charge_window_best, export_window_best, export_limits_best, False, end_record=end_record, save="best"
    )

    # Save plan
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.charge_limit_percent_best = calc_percent_limit(charge_limit_best, my_predbat.soc_max)
    my_predbat.export_limits_best = export_limits_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best

    if len(expect_charge_limit) != len(charge_limit_best):
        print("ERROR: Expected {} charge limits but got {}".format(len(expect_charge_limit), len(charge_limit_best)))
        failed = True
    else:
        for n in range(len(expect_charge_limit)):
            if expect_charge_limit[n] != charge_limit_best[n]:
                print("ERROR: Expected charge limit {} is {} but got {}".format(n, expect_charge_limit[n], charge_limit_best[n]))
                failed = True
    if len(expect_export_limit) != len(export_limits_best):
        print("ERROR: Expected {} discharge limits but got {}".format(len(expect_export_limit), len(export_limits_best)))
        failed = True
    else:
        for n in range(len(expect_export_limit)):
            if expect_export_limit[n] != export_limits_best[n]:
                print("ERROR: Expected discharge limit {} is {} but got {}".format(n, expect_export_limit[n], export_limits_best[n]))
                failed = True

    if abs(expect_best_price - best_price) >= 0.2:
        print("ERROR: Expected best price {} but got {}".format(expect_best_price, best_price))
        failed = True

    if failed:
        my_predbat.publish_html_plan(my_predbat.pv_forecast_minute_step, my_predbat.pv_forecast_minute_step, my_predbat.load_minutes_step, my_predbat.load_minutes_step, end_record)
        open("plan.html", "w").write(my_predbat.html_plan)
        print("Wrote plan to plan.html")

        old_log = my_predbat.log
        my_predbat.log = print
        my_predbat.optimise_charge_limit_price_threads(
            price_set,
            price_links,
            window_index,
            record_charge_windows,
            record_export_windows,
            charge_limit_best,
            charge_window_best,
            export_window_best,
            export_limits_best,
            end_record=end_record,
            fast=True,
            quiet=True,
            test_mode=True,
        )
        my_predbat.log = old_log
        print("Best price: {} Best metric: {} Best cost: {} Best keep: {} Best soc min: {} Best cycle: {} Best carbon: {} Best import: {}".format(best_price, best_metric, best_cost, best_keep, best_soc_min, best_cycle, best_carbon, best_import))
        print("Charge limit best: {} expected {} Discharge limit best {} expected {}".format(charge_limit_best, expect_charge_limit, export_limits_best, expect_export_limit))

    return failed, best_metric, best_keep, charge_limit_best, export_limits_best


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
    reset_rates(my_predbat, import_rate, export_rate)

    failed = False
    failed |= simple_scenario("zero", my_predbat, 0, 0, 0, 0, with_battery=False)
    failed |= simple_scenario("load_only", my_predbat, 1, 0, assert_final_metric=import_rate * 24, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("load_bat_ac", my_predbat, 4, 0, assert_final_metric=import_rate * 24 * 3.2, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8)
    failed |= simple_scenario("load_bat_dc", my_predbat, 4, 0, assert_final_metric=import_rate * 24 * 3.2, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8, hybrid=True)
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
    failed |= simple_scenario("load_car_bat_yes", my_predbat, 1, 0, assert_final_metric=import_rate * 24 * 2, assert_final_soc=100.0 - 24 * 1, with_battery=True, charge_car=2.0, battery_soc=100.0)
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
    failed |= simple_scenario("load_discharge_reserve", my_predbat, 1, 0, assert_final_metric=import_rate * 15, assert_final_soc=1, battery_soc=10.0, with_battery=True, reserve=1.0)
    failed |= simple_scenario("load_discharge_reserve2", my_predbat, 1, 0, assert_final_metric=import_rate * 20, assert_final_soc=2, battery_soc=10.0, with_battery=True, reserve=2.0, battery_loss=0.5)
    failed |= simple_scenario("load_discharge_loss", my_predbat, 1, 0, assert_final_metric=import_rate * 19, assert_final_soc=0, battery_soc=10.0, with_battery=True, battery_loss=0.5)
    failed |= simple_scenario("load_pv", my_predbat, 1, 1, assert_final_metric=0, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("pv_only", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("pv10_only", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False, pv10=True)
    failed |= simple_scenario("pv_only_loss_ac", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False, inverter_loss=0.5)
    failed |= simple_scenario("pv_only_loss_hybrid", my_predbat, 0, 1, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=0, with_battery=False, inverter_loss=0.5, hybrid=True)
    failed |= simple_scenario("pv_only_bat", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_loss", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=12, with_battery=True, battery_loss=0.5)
    failed |= simple_scenario("pv_only_bat_100%", my_predbat, 0, 1, assert_final_metric=-export_rate * 14, assert_final_soc=10, with_battery=True, battery_size=10)
    failed |= simple_scenario("pv_only_bat_ac_clips2", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_ac_clips3", my_predbat, 0, 3, assert_final_metric=-export_rate * 48, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_ac_export_limit", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, export_limit=0.5)
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
    failed |= simple_scenario("pv_only_bat_ac_export_limit_load", my_predbat, 0.5, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, export_limit=0.5)
    failed |= simple_scenario("pv_only_bat_dc_clips2", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, hybrid=True)
    failed |= simple_scenario("pv_only_bat_dc_clips2l", my_predbat, 0, 2, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, inverter_loss=0.5)
    failed |= simple_scenario("pv_only_bat_dc_clips3", my_predbat, 0, 3, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, hybrid=True)
    failed |= simple_scenario("pv_only_bat_dc_clips3l", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, inverter_loss=0.5)
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
    failed |= simple_scenario("pv_only_bat_dc_export_limit", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, export_limit=0.5)
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
    failed |= simple_scenario("pv_only_bat_dc_export_limit_load", my_predbat, 0.5, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, export_limit=0.5)
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
    failed |= simple_scenario("battery_charge_load2", my_predbat, 2, 0, assert_final_metric=import_rate * (34 + 24), assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario("battery_charge_pv", my_predbat, 0, 1, assert_final_metric=-export_rate * 14, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario("battery_charge_pv2", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24, with_battery=True, charge=100, battery_size=100)
    failed |= simple_scenario("battery_charge_pv3", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, charge=100, battery_size=100)
    failed |= simple_scenario("battery_charge_pv4_ac", my_predbat, 0, 2, assert_final_metric=0, assert_final_soc=24, with_battery=True, charge=100, battery_size=100, inverter_loss=0.5, inverter_limit=2)
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
    failed |= simple_scenario("battery_charge_pv_load2_ac", my_predbat, 0.5, 1, assert_final_metric=import_rate * 0.5 * 24, assert_final_soc=24, with_battery=True, charge=100, battery_soc=0)
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
    failed |= simple_scenario("battery_charge_pv_load3_ac", my_predbat, 0.5, 2, assert_final_metric=-export_rate * 0.5 * 24, assert_final_soc=24, with_battery=True, charge=100, battery_soc=0)
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
    failed |= simple_scenario("battery_discharge_pv2_hybrid", my_predbat, 0, 1.5, assert_final_metric=-export_rate * 24, assert_final_soc=22, with_battery=True, discharge=0, battery_soc=10, hybrid=True)
    failed |= simple_scenario("battery_discharge_pv3_hybrid", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, discharge=0, battery_soc=0, hybrid=True)
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
    failed |= simple_scenario("battery_discharge_freeze", my_predbat, 0, 0.5, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=10, with_battery=True, discharge=99, battery_soc=10)
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
    failed |= simple_scenario("battery_charge_ac_loss_pv", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24 * 0.5, with_battery=True, charge=100, battery_size=100, inverter_loss=0.5)
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
    failed |= simple_scenario(
        "iboost_pv",
        my_predbat,
        0,
        1,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_solar=True,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    failed |= simple_scenario(
        "iboost_gas1",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_gas=True,
        rate_gas=5.0,
        gas_scale=0.8,
        iboost_charging=False,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_gas2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_gas=True,
        rate_gas=10.0,
        gas_scale=1.2,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_gas3",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_gas_export=True,
        rate_gas=4.0,
        gas_scale=1.2,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_gas4",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_gas_export=True,
        rate_gas=5.0,
        gas_scale=1.2,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_rate1",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold=import_rate * 0.9,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_rate2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_rate3",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 200,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold_export=export_rate,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=200,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_rate3",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold_export=export_rate - 1,
        iboost_charging=False,
        export_limit=10,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_charge1",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * (10 + 12),
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_enable=True,
        iboost_charging=True,
        assert_final_iboost=12,
        charge_period_divide=2,
        export_limit=1,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
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
        iboost_enable=True,
        iboost_charging=True,
        assert_final_iboost=100,
        end_record=12 * 60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_charge3",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * (10 * 10 + 10),
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_enable=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=True,
        assert_final_iboost=100,
        end_record=12 * 60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_charge4",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 10,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
        iboost_enable=True,
        iboost_rate_threshold=import_rate - 1,
        iboost_charging=True,
        assert_final_iboost=0,
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
        iboost_enable=True,
        iboost_charging=True,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_discharge2",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 24,
        assert_final_soc=100 - 24,
        battery_soc=100,
        with_battery=True,
        discharge=0,
        battery_size=100,
        iboost_enable=True,
        export_limit=1,
        assert_final_iboost=0,
    )
    failed |= simple_scenario(
        "iboost_discharge3",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=100 - 24,
        battery_soc=100,
        with_battery=True,
        discharge=0,
        battery_size=100,
        iboost_enable=True,
        iboost_on_export=True,
        export_limit=1,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_prevent_discharge1",
        my_predbat,
        0,
        0,
        assert_final_metric=0,
        assert_final_soc=100 - 24,
        battery_soc=100,
        with_battery=True,
        battery_size=100,
        iboost_enable=True,
        iboost_on_export=True,
        iboost_prevent_discharge=False,
        export_limit=1,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_prevent_discharge2",
        my_predbat,
        0,
        0,
        assert_final_metric=24 * import_rate,
        assert_final_soc=100,
        battery_soc=100,
        with_battery=True,
        battery_size=100,
        iboost_enable=True,
        iboost_on_export=True,
        iboost_prevent_discharge=True,
        export_limit=1,
        assert_final_iboost=24,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
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
    reset_rates2(my_predbat, import_rate, export_rate)
    failed |= simple_scenario(
        "iboost_rate3",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 120,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=120,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_rate_pv1",
        my_predbat,
        0,
        1.0,
        assert_final_metric=-export_rate * 12 * 2,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_solar=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=12,
        export_limit=1,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    failed |= simple_scenario(
        "iboost_rate_pv2",
        my_predbat,
        0,
        1.0,
        assert_final_metric=-export_rate * 12 * 2,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_solar=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=12 * 1,
        export_limit=2,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )
    failed |= simple_scenario(
        "iboost_rate_pv3",
        my_predbat,
        0,
        2.0,
        assert_final_metric=-export_rate * 12 * 2 * 2,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_solar=True,
        iboost_rate_threshold=import_rate,
        iboost_charging=False,
        assert_final_iboost=12 * 2,
        export_limit=2,
        assert_iboost_running=True,
        assert_iboost_running_solar=True,
    )

    if failed:
        print("**** ERROR: Some Model tests failed ****")
    return failed


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Predbat unit tests")
    parser.add_argument("--debug_file", action="store", help="Enable debug output")
    args = parser.parse_args()

    print("**** Starting Predbat tests ****")
    my_predbat = PredBat()
    my_predbat.states = {}
    my_predbat.reset()
    my_predbat.update_time()
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.ha_interface.history_enable = False
    my_predbat.auto_config()
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.ha_interface.history_enable = True
    my_predbat.expose_config("plan_debug", True)
    print("**** Testing Predbat ****")
    failed = False

    if args.debug_file:
        run_single_debug(my_predbat, args.debug_file)
        sys.exit(0)

    free_sessions = my_predbat.download_octopus_free("http://octopus.energy/free-electricity")
    free_sessions = my_predbat.download_octopus_free("http://octopus.energy/free-electricity")
    if not free_sessions:
        print("**** ERROR: No free sessions found ****")
        failed = 1
    if not failed:
        failed |= run_intersect_window_tests(my_predbat)
    if not failed:
        failed |= run_execute_tests(my_predbat)
    if not failed:
        failed |= run_inverter_multi_tests(my_predbat)
    if not failed:
        failed |= run_model_tests(my_predbat)
    if not failed:
        failed |= run_window_sort_tests(my_predbat)
    if not failed:
        failed |= run_optimise_levels_tests(my_predbat)
    if not failed:
        failed |= run_optimise_all_windows_tests(my_predbat)
    if not failed:
        failed |= run_compute_metric_tests(my_predbat)
    if not failed:
        failed |= run_perf_test(my_predbat)
    if not failed:
        failed |= run_nordpool_test(my_predbat)

    if failed:
        print("**** ERROR: Some tests failed ****")
        sys.exit(1)
    print("**** Tests passed ****")
    sys.exit(0)


if __name__ == "__main__":
    main()
