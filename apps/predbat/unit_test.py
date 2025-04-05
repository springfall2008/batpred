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
import glob
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
from utils import calc_percent_limit, remove_intersecting_windows, find_charge_rate, dp4
from futurerate import FutureRate
from config import PREDICT_STEP, MINUTE_WATT
from inverter import Inverter
from config import INVERTER_DEF
from compare import Compare
from web import WebInterface
from gecloud import GECloudDirect
from octopus import OctopusAPI

# Import MagicMock
from unittest.mock import MagicMock

KEEP_SCALE = 0.5


class TestHAInterface:
    def __init__(self):
        self.step = 5
        self.build_history()
        self.history_enable = True
        self.dummy_items = {}
        self.service_store_enable = False
        self.service_store = []
        self.db_primary = False

    def get_service_store(self):
        stored_service = self.service_store
        self.service_store = []
        return stored_service

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
        elif entity_id in self.dummy_items:
            result = self.dummy_items[entity_id]
            if isinstance(result, dict):
                if attribute:
                    result = result.get(attribute, default)
                else:
                    result = result.get("state", default)
            else:
                if attribute:
                    result = default
            print("Getting state: {} attribute {} => {}".format(entity_id, attribute, result))
            return result
        else:
            print("Getting state: {} attribute {} => default".format(entity_id, default))
            return default

    def call_service(self, service, **kwargs):
        print("Calling service: {} {}".format(service, kwargs))
        if self.service_store_enable:
            self.service_store.append([service, kwargs])
            return None

        if service == "number/set_value":
            entity_id = kwargs.get("entity_id", None)
            if not entity_id.startswith("number."):
                print("Warn: Service for entity {} not a number".format(entity_id))
            elif entity_id in self.dummy_items:
                print("Setting state: {} to {}".format(entity_id, kwargs.get("value", 0)))
                self.dummy_items[entity_id] = kwargs.get("value", 0)
            else:
                print("Warn: Service for entity {} not found".format(entity_id))
        elif service == "switch/turn_on":
            entity_id = kwargs.get("entity_id", None)
            if not entity_id.startswith("switch."):
                print("Warn: Service for entity {} not a switch".format(entity_id))
            elif entity_id in self.dummy_items:
                self.dummy_items[entity_id] = "on"
        elif service == "switch/turn_off":
            entity_id = kwargs.get("entity_id", None)
            if not entity_id.startswith("switch."):
                print("Warn: Service for entity {} not a switch".format(entity_id))
            elif entity_id in self.dummy_items:
                self.dummy_items[entity_id] = "off"
        elif service == "select/select_option":
            entity_id = kwargs.get("entity_id", None)
            if not entity_id.startswith("select."):
                print("Warn: Service for entity {} not a select".format(entity_id))
            elif entity_id in self.dummy_items:
                self.dummy_items[entity_id] = kwargs.get("option", None)
        return None

    def set_state(self, entity_id, state, attributes=None):
        # print("Setting state: {} to {} attributes {}".format(entity_id, state, str(attributes)))
        self.dummy_items[entity_id] = state
        return None

    def get_history(self, entity_id, now=None, days=30):
        # print("Getting history for {}".format(entity_id))
        if entity_id == "predbat.status":
            return [[{"state": "idle", "last_changed": datetime.now()}]]
        if self.history_enable:
            return [self.history]
        else:
            return None


class TestInverter:
    def __init__(self):
        self.id = 0
        pass


def reset_rates(my_predbat, ir, xr):
    my_predbat.combine_charge_slots = True
    for minute in range(my_predbat.forecast_minutes + my_predbat.minutes_now):
        my_predbat.rate_import[minute] = ir
        my_predbat.rate_export[minute] = xr
    my_predbat.rate_export_min = xr
    my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_scan(my_predbat.rate_export, print=False)


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
    my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_scan(my_predbat.rate_export, print=False)


def update_rates_import(my_predbat, charge_window_best):
    for window in charge_window_best:
        for minute in range(window["start"], window["end"]):
            my_predbat.rate_import[minute] = window["average"]
    my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_scan(my_predbat.rate_export, print=False)


def update_rates_export(my_predbat, export_window_best):
    for window in export_window_best:
        for minute in range(window["start"], window["end"]):
            my_predbat.rate_export[minute] = window["average"]
    my_predbat.rate_scan(my_predbat.rate_import, print=False)
    my_predbat.rate_scan(my_predbat.rate_export, print=False)


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
    my_predbat.reserve_percent_current = 0.0
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
    my_predbat.car_charging_soc = [0, 0, 0, 0]
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

    metric, battery_value = my_predbat.compute_metric(
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

    fixed = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/OUTGOING-FIX-12M-BB-23-02-09/electricity-tariffs/E-1R-OUTGOING-FIX-12M-BB-23-02-09-A/standard-unit-rates/")
    if max(fixed.values()) <= 0:
        print("ERROR: Fixed rates can not be zero")
        failed = True
    if min(fixed.values()) != max(fixed.values()):
        print("ERROR: Fixed rates can can change")
        failed = True
    if len(fixed) > 5 * 24 * 60:
        print("ERROR: Fixed rates too long got {}".format(len(fixed)))
        failed = True

    # Obtain Agile octopus data
    rates_agile = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-A/standard-unit-rates/")
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
            print("ERROR: Rate import data not the same got {} vs {}".format(rate_import[key], rate_import2.get(key, None)))
            failed = True
            break
    for key in rate_export:
        if rate_export[key] != rate_export2.get(key, None):
            print("ERROR: Rate export data not the same got {} vs {}".format(rate_export[key], rate_export2.get(key, None)))
            failed = True
            break

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
    if min_export < 0 or max_export > 100:
        print("ERROR: Rate export data out of range got min {} max {}".format(min_export, max_export))
        failed = True

    # Compare Agile rates against Nordpool
    max_diff = 0
    rate_diff = 0
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


def dummy_sleep(seconds):
    """
    Dummy sleep function
    """
    pass


class DummyRestAPI:
    def __init__(self):
        self.commands = []
        self.rest_data = {}
        self.queued_rest = []

    def queue_rest_data(self, data):
        # print("Queue rest data {}".format(data))
        self.queued_rest.append(copy.deepcopy(data))

    def clear_queue(self):
        self.queued_rest = []

    def dummy_rest_postCommand(self, url, json):
        """
        Dummy rest post command
        """
        # print("Dummy rest post command {} {}".format(url, json))
        self.commands.append([url, json])

    def dummy_rest_getData(self, url):
        if url == "dummy/runAll":
            if self.queued_rest:
                self.rest_data = self.queued_rest.pop(0)
            # print("Dummy rest get data {} returns {}".format(url, self.rest_data))
            return self.rest_data
        elif url == "dummy/readData":
            # print("Dummy rest get data {} returns {}".format(url, self.rest_data))
            return self.rest_data
        else:
            return None

    def get_commands(self):
        commands = self.commands
        self.commands = []
        return commands


def test_adjust_charge_window(test_name, ha, inv, dummy_rest, prev_charge_start_time, prev_charge_end_time, prev_enable_charge, charge_start_time, charge_end_time, minutes_now, short=False):
    """
    test:
        inv.adjust_charge_window(self, charge_start_time, charge_end_time, minutes_now):
    """
    failed = False
    print("Test: {}".format(test_name))

    inv.rest_data = None
    ha.dummy_items["select.charge_start_time"] = prev_charge_start_time[:5] if short else prev_charge_start_time
    ha.dummy_items["select.charge_end_time"] = prev_charge_end_time[:5] if short else prev_charge_end_time
    ha.dummy_items["switch.scheduled_charge_enable"] = "on" if prev_enable_charge else "off"
    charge_start_time_tm = datetime.strptime(charge_start_time, "%H:%M:%S")
    charge_end_time_tm = datetime.strptime(charge_end_time, "%H:%M:%S")

    inv.adjust_charge_window(charge_start_time_tm, charge_end_time_tm, minutes_now)

    if short:
        expect_charge_start_time = charge_start_time[:5]
        expect_charge_end_time = charge_end_time[:5]
    else:
        expect_charge_start_time = charge_start_time
        expect_charge_end_time = charge_end_time

    if ha.get_state("select.charge_start_time") != expect_charge_start_time:
        print("ERROR: Charge start time should be {} got {}".format(expect_charge_start_time, ha.get_state("select.charge_start_time")))
        failed = True
    if ha.get_state("select.charge_end_time") != expect_charge_end_time:
        print("ERROR: Charge end time should be {} got {}".format(expect_charge_end_time, ha.get_state("select.charge_end_time")))
        failed = True
    if ha.get_state("switch.scheduled_charge_enable") != "on":
        print("ERROR: Charge enable should be on got {}".format(ha.get_state("switch.scheduled_charge_enable")))
        failed = True

    # REST Mode
    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Timeslots"] = {}
    inv.rest_data["Timeslots"]["Charge_start_time_slot_1"] = prev_charge_start_time
    inv.rest_data["Timeslots"]["Charge_end_time_slot_1"] = prev_charge_end_time
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"]["Enable_Charge_Schedule"] = "on" if prev_enable_charge else "off"
    dummy_rest.rest_data = copy.deepcopy(inv.rest_data)
    dummy_rest.rest_data["Timeslots"]["Charge_start_time_slot_1"] = charge_start_time
    dummy_rest.rest_data["Timeslots"]["Charge_end_time_slot_1"] = charge_end_time
    dummy_rest.rest_data["Control"]["Enable_Charge_Schedule"] = True

    inv.adjust_charge_window(charge_start_time_tm, charge_end_time_tm, minutes_now)
    rest_command = dummy_rest.get_commands()
    if prev_charge_start_time != charge_start_time or prev_charge_end_time != charge_end_time:
        expect_data = [["dummy/setChargeSlot1", {"start": charge_start_time[0:5], "finish": charge_end_time[0:5]}]]
    else:
        expect_data = []
    if prev_enable_charge != True:
        expect_data.append(["dummy/enableChargeSchedule", {"state": "enable"}])

    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True
    return failed


def test_adjust_reserve(test_name, ha, inv, dummy_rest, prev_reserve, reserve, expect_reserve=None, reserve_min=4, reserve_max=100):
    """
    Test
       inv.adjust_reserve(self, reserve):
    """
    failed = False
    if expect_reserve is None:
        expect_reserve = reserve

    inv.reserve_percent = reserve_min
    inv.reserve_max = reserve_max

    print("Test: {}".format(test_name))

    # Non-REST Mode
    inv.rest_data = None
    ha.dummy_items["number.reserve"] = prev_reserve
    inv.adjust_reserve(reserve)
    if ha.get_state("number.reserve") != expect_reserve:
        print("ERROR: Reserve should be {} got {}".format(expect_reserve, ha.get_state("number.reserve")))
        failed = True

    # REST Mode
    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"]["Battery_Power_Reserve"] = prev_reserve
    dummy_rest.rest_data = copy.deepcopy(inv.rest_data)
    dummy_rest.rest_data["Control"]["Battery_Power_Reserve"] = expect_reserve

    inv.adjust_reserve(reserve)
    rest_command = dummy_rest.get_commands()
    if prev_reserve != expect_reserve:
        expect_data = [["dummy/setBatteryReserve", {"reservePercent": expect_reserve}]]
    else:
        expect_data = []
    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True

    return failed


def test_adjust_force_export(test_name, ha, inv, dummy_rest, prev_start, prev_end, prev_force_export, prev_discharge_target, new_start, new_end, new_force_export):
    """
    Test
       inv.adjust_reserve(self, reserve):
    """
    failed = False

    print("Test: {} - non-REST".format(test_name))

    if new_start is None:
        new_start = prev_start
    if new_end is None:
        new_end = prev_end

    prev_mode = "Timed Export" if prev_force_export else "Eco"
    new_mode = "Timed Export" if new_force_export else "Eco"
    prev_force_export = "on" if prev_force_export else "off"
    export_schedule_discharge = "on" if new_force_export else "off"

    # Non-REST Mode
    inv.rest_data = None
    inv.reserve_precent = 4
    inv.inv_has_charge_enable_time = False
    inv.ge_inverter_mode = True
    inv.rest_v3 = True

    if inv.ge_inverter_mode and not new_force_export:
        expect_start = prev_start
        expect_end = prev_end
    else:
        expect_start = new_start
        expect_end = new_end

    new_discharge_target = inv.reserve_precent if new_force_export else prev_discharge_target

    ha.dummy_items["select.discharge_start_time"] = prev_start
    ha.dummy_items["select.discharge_end_time"] = prev_end
    ha.dummy_items["sensor.predbat_GE_0_scheduled_discharge_enable"] = prev_force_export
    ha.dummy_items["number.discharge_target_soc"] = prev_discharge_target
    ha.dummy_items["select.inverter_mode"] = prev_mode

    new_start_timestamp = datetime.strptime(new_start, "%H:%M:%S")
    new_end_timestamp = datetime.strptime(new_end, "%H:%M:%S")
    inv.adjust_force_export(new_force_export, new_start_timestamp, new_end_timestamp)

    if ha.get_state("sensor.predbat_GE_0_scheduled_discharge_enable") != export_schedule_discharge:
        print("ERROR: scheduled discharge enable should be {} got {}".format(export_schedule_discharge, ha.get_state("sensor.predbat_GE_0_scheduled_discharge_enable")))
        failed = True
    if ha.get_state("select.discharge_start_time") != expect_start:
        print("ERROR: Discharge start time should be {} got {}".format(new_start, ha.get_state("select.discharge_start_time")))
        failed = True
    if ha.get_state("select.discharge_end_time") != expect_end:
        print("ERROR: Discharge end time should be {} got {}".format(new_end, ha.get_state("select.discharge_end_time")))
        failed = True
    if ha.get_state("number.discharge_target_soc") != new_discharge_target:
        print("ERROR: Discharge target soc should be {} got {}".format(new_discharge_target, ha.get_state("number.discharge_target_soc")))
        failed = True
    if ha.get_state("select.inverter_mode") != new_mode:
        print("ERROR: Inverter mode should be {} got {}".format(new_mode, ha.get_state("select.inverter_mode")))
        failed = True

    print("Test: {} - REST".format(test_name))
    # REST Mode
    inv.rest_api = "dummy"
    inv.reserve_precent = 4
    inv.inv_has_charge_enable_time = False

    inv.rest_data = {}
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"]["Enable_Discharge_Schedule"] = prev_force_export
    inv.rest_data["Control"]["Mode"] = prev_mode
    inv.rest_data["Timeslots"] = {}
    inv.rest_data["Timeslots"]["Discharge_start_time_slot_1"] = prev_start
    inv.rest_data["Timeslots"]["Discharge_end_time_slot_1"] = prev_end
    inv.rest_data["raw"] = {}
    inv.rest_data["raw"]["invertor"] = {}
    inv.rest_data["raw"]["invertor"]["discharge_target_soc_1"] = prev_discharge_target

    dummy_rest.clear_queue()
    dummy1 = copy.deepcopy(inv.rest_data)

    dummy1["raw"]["invertor"]["discharge_target_soc_1"] = inv.reserve_precent if new_force_export else prev_discharge_target
    if new_discharge_target != prev_discharge_target:
        dummy_rest.queue_rest_data(dummy1)

    dummy1["Timeslots"]["Discharge_start_time_slot_1"] = new_start
    dummy1["Timeslots"]["Discharge_end_time_slot_1"] = new_end
    if prev_start != expect_start or prev_end != expect_end:
        dummy_rest.queue_rest_data(dummy1)

    dummy1["Control"]["Mode"] = new_mode
    dummy1["Control"]["Enable_Discharge_Schedule"] = export_schedule_discharge
    if prev_mode != new_mode:
        dummy_rest.queue_rest_data(dummy1)

    dummy_rest.rest_data = copy.deepcopy(dummy1)

    new_start_timestamp = datetime.strptime(new_start, "%H:%M:%S")
    new_end_timestamp = datetime.strptime(new_end, "%H:%M:%S")

    print("Inv prev mode {} new mode {}".format(prev_mode, new_mode))
    print(dummy_rest.rest_data)
    print(inv.rest_data)
    inv.adjust_force_export(new_force_export, new_start_timestamp, new_end_timestamp)

    rest_command = dummy_rest.get_commands()
    expect_data = []
    if new_discharge_target != prev_discharge_target:
        expect_data.append(["dummy/setDischargeTarget", {"dischargeToPercent": int(new_discharge_target), "slot": 1}])

    if prev_start != expect_start or prev_end != expect_end:
        expect_data.append(["dummy/setDischargeSlot1", {"start": expect_start[0:5], "finish": expect_end[0:5]}])

    if prev_mode != new_mode:
        expect_data.append(["dummy/setBatteryMode", {"mode": new_mode}])

    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True

    return failed


def test_adjust_charge_rate(test_name, ha, inv, dummy_rest, prev_rate, rate, expect_rate=None, discharge=False):
    """
    Test the adjust_inverter_mode function
    """
    failed = False
    if expect_rate is None:
        expect_rate = rate

    print("Test: {} prev_rate {} rate {} expect_rate {}".format(test_name, prev_rate, rate, expect_rate))

    # Non-REST Mode
    inv.rest_data = None
    inv.rest_api = None
    entity = "number.discharge_rate" if discharge else "number.charge_rate"
    entity_percent = "number.discharge_rate_percent" if discharge else "number.charge_rate_percent"
    expect_percent = int(expect_rate * 100 / inv.battery_rate_max_raw)
    ha.dummy_items[entity] = prev_rate
    ha.dummy_items[entity_percent] = int(prev_rate * 100 / inv.battery_rate_max_raw)
    if discharge:
        inv.adjust_discharge_rate(rate)
    else:
        inv.adjust_charge_rate(rate)
    if ha.get_state(entity) != expect_rate:
        print("ERROR: Inverter rate should be {} got {}".format(expect_rate, ha.get_state(entity)))
        failed = True
    if ha.get_state(entity_percent) != expect_percent:
        print("ERROR: Inverter rate percent should be {} got {} - rate {} max_rate_raw {}".format(expect_percent, ha.get_state(entity_percent), rate, inv.battery_rate_max_raw))
        failed = True

    # REST Mode
    rest_entity = "Battery_Discharge_Rate" if discharge else "Battery_Charge_Rate"
    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"][rest_entity] = prev_rate
    dummy_rest.rest_data = copy.deepcopy(inv.rest_data)
    dummy_rest.rest_data["Control"][rest_entity] = expect_rate

    rest_command = dummy_rest.get_commands()
    if rest_command:
        print("ERROR Previous was command was not cleared, started with:".format(rest_command))
        failed = True

    if discharge:
        inv.adjust_discharge_rate(rate)
    else:
        inv.adjust_charge_rate(rate)

    rest_command = dummy_rest.get_commands()
    if prev_rate != expect_rate:
        print("Prev_rate {} expect_rate {}".format(prev_rate, expect_rate))
        if discharge:
            expect_data = [["dummy/setDischargeRate", {"dischargeRate": expect_rate}]]
        else:
            expect_data = [["dummy/setChargeRate", {"chargeRate": expect_rate}]]
    else:
        expect_data = []
    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True

    return failed


def test_adjust_inverter_mode(test_name, ha, inv, dummy_rest, prev_mode, mode, expect_mode=None):
    """
    Test the adjust_inverter_mode function
    """
    failed = False
    if expect_mode is None:
        expect_mode = mode

    print("Test: {}".format(test_name))

    # Non-REST Mode
    inv.rest_data = None
    ha.dummy_items["select.inverter_mode"] = prev_mode
    inv.adjust_inverter_mode(True if mode == "Timed Export" else False, False)
    if ha.get_state("select.inverter_mode") != expect_mode:
        print("ERROR: Inverter mode should be {} got {}".format(expect_mode, ha.get_state("select.inverter_mode")))
        failed = True

    # REST Mode
    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"]["Mode"] = prev_mode
    dummy_rest.rest_data = copy.deepcopy(inv.rest_data)
    dummy_rest.rest_data["Control"]["Mode"] = expect_mode

    inv.adjust_inverter_mode(True if mode == "Timed Export" else False, False)
    rest_command = dummy_rest.get_commands()
    if prev_mode != expect_mode:
        expect_data = [["dummy/setBatteryMode", {"mode": expect_mode}]]
    else:
        expect_data = []
    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True

    return failed


def test_adjust_battery_target(test_name, ha, inv, dummy_rest, prev_soc, soc, isCharging, isExporting, expect_soc=None):
    """
    Test the adjust_battery_target function
    """
    failed = False
    if expect_soc is None:
        expect_soc = soc

    print("Test: {}".format(test_name))

    # Non-REST Mode
    inv.rest_data = None
    ha.dummy_items["number.charge_limit"] = prev_soc
    inv.adjust_battery_target(soc, isCharging=True, isExporting=False)
    if ha.get_state("number.charge_limit") != expect_soc:
        print("ERROR: Charge limit should be {} got {}".format(expect_soc, ha.get_state("number.charge_limit")))
        failed = True

    # REST Mode
    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"]["Target_SOC"] = prev_soc
    dummy_rest.rest_data = copy.deepcopy(inv.rest_data)
    dummy_rest.rest_data["Control"]["Target_SOC"] = expect_soc

    inv.adjust_battery_target(soc, isCharging=True, isExporting=False)
    rest_command = dummy_rest.get_commands()
    if soc != prev_soc:
        expect_data = [["dummy/setChargeTarget", {"chargeToPercent": expect_soc}]]
    else:
        expect_data = []
    if json.dumps(expect_data) != json.dumps(rest_command):
        print("ERROR: Rest command should be {} got {}".format(expect_data, rest_command))
        failed = True

    return failed


def test_inverter_rest_template(
    test_name,
    my_predbat,
    filename,
    assert_soc_max=9.52,
    assert_soc=0,
    assert_voltage=52,
    assert_inverter_limit=3600,
    assert_battery_rate_max=2600,
    assert_serial_number="Unknown",
    assert_pv_power=0,
    assert_load_power=0,
    assert_charge_start_time_minutes=0,
    assert_charge_end_time_minutes=0,
    assert_charge_enable=False,
    assert_discharge_start_time_minutes=0,
    assert_discharge_end_time_minutes=0,
    assert_discharge_enable=False,
    assert_pause_start_time_minutes=0,
    assert_pause_end_time_minutes=0,
    assert_nominal_capacity=9.52,
    assert_battery_temperature=0,
):
    failed = False
    print("**** Running Test: {} ****".format(test_name))
    dummy_rest = DummyRestAPI()
    my_predbat.args["givtcp_rest"] = "dummy"

    dummy_rest.rest_data = {}
    with open(filename, "r") as file:
        dummy_rest.rest_data = json.load(file)

    my_predbat.restart_active = True
    inv = Inverter(my_predbat, 0, rest_postCommand=dummy_rest.dummy_rest_postCommand, rest_getData=dummy_rest.dummy_rest_getData, quiet=False)
    inv.sleep = dummy_sleep

    inv.update_status(my_predbat.minutes_now)
    my_predbat.restart_active = False

    if assert_soc_max != inv.soc_max:
        print("ERROR: SOC Max should be {} got {}".format(assert_soc_max, inv.soc_max))
        failed = True
    if assert_soc != inv.soc_kw:
        print("ERROR: SOC should be {} got {}".format(assert_soc, inv.soc_kw))
        failed = True
    if assert_voltage != inv.battery_voltage:
        print("ERROR: Voltage should be {} got {}".format(assert_voltage, inv.battery_voltage))
        failed = True
    if assert_inverter_limit != inv.inverter_limit * MINUTE_WATT:
        print("ERROR: Inverter limit should be {} got {}".format(assert_inverter_limit, inv.inverter_limit * MINUTE_WATT))
        failed = True
    if assert_battery_rate_max != inv.battery_rate_max_raw:
        print("ERROR: Battery rate max should be {} got {}".format(assert_battery_rate_max, inv.battery_rate_max_raw))
        failed = True
    if assert_serial_number != inv.serial_number:
        print("ERROR: Serial number should be {} got {}".format(assert_serial_number, inv.serial_number))
        failed = True
    if assert_pv_power != inv.pv_power:
        print("ERROR: PV power should be {} got {}".format(assert_pv_power, inv.pv_power))
        failed = True
    if assert_load_power != inv.load_power:
        print("ERROR: Load power should be {} got {}".format(assert_load_power, inv.load_power))
        failed = True
    if assert_charge_start_time_minutes != inv.charge_start_time_minutes:
        print("ERROR: Charge start time should be {} got {}".format(assert_charge_start_time_minutes, inv.charge_start_time_minutes))
        failed = True
    if assert_charge_end_time_minutes != inv.charge_end_time_minutes:
        print("ERROR: Discharge end time should be {} got {}".format(assert_charge_end_time_minutes, inv.charge_end_time_minutes))
        failed = True
    if assert_charge_enable != inv.charge_enable_time:
        print("ERROR: Charge enable should be {} got {}".format(assert_charge_enable, inv.charge_enable_time))
        failed = True
    if assert_discharge_start_time_minutes != inv.discharge_start_time_minutes:
        print("ERROR: Discharge start time should be {} got {}".format(assert_discharge_start_time_minutes, inv.discharge_start_time_minutes))
        failed = True
    if assert_discharge_end_time_minutes != inv.discharge_end_time_minutes:
        print("ERROR: Discharge end time should be {} got {}".format(assert_discharge_end_time_minutes, inv.discharge_end_time_minutes))
        failed = True
    if assert_discharge_enable != inv.discharge_enable_time:
        print("ERROR: Discharge enable should be {} got {}".format(assert_discharge_enable, inv.discharge_enable_time))
        failed = True
    if assert_nominal_capacity != inv.nominal_capacity:
        print("ERROR: Nominal capacity should be {} got {}".format(assert_nominal_capacity, inv.nominal_capacity))
        failed = True
    if assert_battery_temperature != inv.battery_temperature:
        print("ERROR: Battery temperature should be {} got {}".format(assert_battery_temperature, inv.battery_temperature))
        failed = True

    return failed


def test_inverter_update(
    test_name,
    my_predbat,
    dummy_items,
    expect_charge_start_time,
    expect_charge_end_time,
    expect_charge_enable,
    expect_discharge_start_time,
    expect_discharge_end_time,
    expect_discharge_enable,
    expect_battery_power,
    expect_pv_power,
    expect_load_power,
    expect_soc_kwh,
    soc_percent=False,
    expect_battery_capacity=10.0,
    has_charge_enable_time=True,
    has_discharge_enable_time=True,
):
    failed = False
    print("**** Running Test: {} ****".format(test_name))

    midnight = datetime.strptime("00:00:00", "%H:%M:%S")
    charge_start_time_minutes = (datetime.strptime(expect_charge_start_time, "%H:%M:%S") - midnight).total_seconds() / 60
    charge_end_time_minutes = (datetime.strptime(expect_charge_end_time, "%H:%M:%S") - midnight).total_seconds() / 60
    discharge_start_time_minutes = (datetime.strptime(expect_discharge_start_time, "%H:%M:%S") - midnight).total_seconds() / 60
    discharge_end_time_minutes = (datetime.strptime(expect_discharge_end_time, "%H:%M:%S") - midnight).total_seconds() / 60

    if charge_end_time_minutes < charge_start_time_minutes:
        if charge_end_time_minutes < my_predbat.minutes_now:
            charge_end_time_minutes += 60 * 24
        else:
            charge_start_time_minutes -= 60 * 24

    if discharge_end_time_minutes < discharge_start_time_minutes:
        if discharge_end_time_minutes < my_predbat.minutes_now:
            discharge_end_time_minutes += 60 * 24
        else:
            discharge_start_time_minutes -= 60 * 24

    my_predbat.args["givtcp_rest"] = None
    inv = Inverter(my_predbat, 0)
    inv.sleep = dummy_sleep

    inv.charge_start_time_minutes = -1
    inv.charge_end_time_minutes = -1
    inv.charge_enable_time = False
    inv.discharge_start_time_minutes = -1
    inv.discharge_end_time_minutes = -1
    inv.discharge_enable_time = False
    inv.battery_power = 0
    inv.pv_power = 0
    inv.load_power = 0
    inv.soc_kw = 0
    inv.inv_has_charge_enable_time = has_charge_enable_time
    inv.inv_has_discharge_enable_time = has_discharge_enable_time

    print("Test: Update Inverter")

    dummy_items["select.charge_start_time"] = expect_charge_start_time
    dummy_items["select.charge_end_time"] = expect_charge_end_time
    dummy_items["select.discharge_start_time"] = expect_discharge_start_time
    dummy_items["select.discharge_end_time"] = expect_discharge_end_time
    dummy_items["sensor.battery_power"] = expect_battery_power
    dummy_items["sensor.pv_power"] = expect_pv_power
    dummy_items["sensor.load_power"] = expect_load_power
    dummy_items["switch.scheduled_charge_enable"] = "on" if expect_charge_enable else "off"
    dummy_items["switch.scheduled_discharge_enable"] = "on" if expect_discharge_enable else "off"
    dummy_items["number.discharge_target_soc"] = 4
    dummy_items["sensor.battery_capacity"] = expect_battery_capacity
    dummy_items["sensor.predbat_GE_0_scheduled_discharge_enable"] = "on" if expect_discharge_enable else "off"
    print("sensor.predbat_GE_0_scheduled_discharge_enable = {}".format(dummy_items["sensor.predbat_GE_0_scheduled_discharge_enable"]))
    if not has_discharge_enable_time:
        dummy_items["switch.scheduled_discharge_enable"] = "n/a"

    if soc_percent:
        dummy_items["sensor.soc_kw"] = -1
        dummy_items["sensor.soc_percent"] = calc_percent_limit(expect_soc_kwh, expect_battery_capacity)
        if "soc_kw" in my_predbat.args:
            del my_predbat.args["soc_kw"]
        my_predbat.args["soc_percent"] = "sensor.soc_percent"
    else:
        dummy_items["sensor.soc_kw"] = expect_soc_kwh
        dummy_items["sensor.soc_percent"] = -1
        my_predbat.args["soc_kw"] = "sensor.soc_kw"
        if "soc_percent" in my_predbat.args:
            del my_predbat.args["soc_percent"]

    inv.update_status(my_predbat.minutes_now)
    if not has_charge_enable_time:
        if charge_start_time_minutes == charge_end_time_minutes:
            expect_charge_enable = False
        else:
            expect_charge_enable = True

    if not has_discharge_enable_time:
        if discharge_start_time_minutes == discharge_end_time_minutes:
            expect_discharge_enable = False
        else:
            expect_discharge_enable = True
        print("Set expect_discharge_enable to {}".format(expect_discharge_enable))

    if not expect_charge_enable:
        charge_start_time_minutes = 0
        charge_end_time_minutes = 0

    if charge_end_time_minutes < my_predbat.minutes_now:
        charge_start_time_minutes += 24 * 60
        charge_end_time_minutes += 24 * 60
    if discharge_end_time_minutes < my_predbat.minutes_now:
        discharge_start_time_minutes += 24 * 60
        discharge_end_time_minutes += 24 * 60

    if inv.charge_start_time_minutes != charge_start_time_minutes:
        print("ERROR: Charge start time should be {} got {} ({})".format(charge_start_time_minutes, inv.charge_start_time_minutes, dummy_items["select.charge_start_time"]))
        failed = True
    if inv.charge_end_time_minutes != charge_end_time_minutes:
        print("ERROR: Charge end time should be {} got {}".format(charge_end_time_minutes, inv.charge_end_time_minutes))
        failed = True
    if inv.charge_enable_time != expect_charge_enable:
        print("ERROR: Charge enable should be {} got {}".format(expect_charge_enable, inv.charge_enable_time))
        failed = True
    if inv.discharge_start_time_minutes != discharge_start_time_minutes:
        print("ERROR: Discharge start time should be {} got {}".format(discharge_start_time_minutes, inv.discharge_start_time_minutes))
        failed = True
    if inv.discharge_end_time_minutes != discharge_end_time_minutes:
        print("ERROR: Discharge end time should be {} got {}".format(discharge_end_time_minutes, inv.discharge_end_time_minutes))
        failed = True
    if inv.discharge_enable_time != expect_discharge_enable:
        print("ERROR: Discharge enable should be {} got {}".format(expect_discharge_enable, inv.discharge_enable_time))
        failed = True
    if inv.battery_power != expect_battery_power:
        print("ERROR: Battery power should be {} got {}".format(expect_battery_power, inv.battery_power))
        failed = True
    if inv.pv_power != expect_pv_power:
        print("ERROR: PV power should be {} got {}".format(expect_pv_power, inv.pv_power))
        failed = True
    if inv.load_power != expect_load_power:
        print("ERROR: Load power should be {} got {}".format(expect_load_power, inv.load_power))
        failed = True
    if inv.soc_kw != expect_soc_kwh:
        print("ERROR: SOC kWh should be {} got {}".format(expect_soc_kwh, inv.soc_kw))
        failed = True

    # REST Mode

    dummy_rest = DummyRestAPI()
    my_predbat.args["givtcp_rest"] = "dummy"

    dummy_rest.rest_data = {}
    dummy_rest.rest_data["Control"] = {}
    dummy_rest.rest_data["Control"]["Target_SOC"] = 99
    dummy_rest.rest_data["Control"]["Mode"] = "Eco"
    dummy_rest.rest_data["Control"]["Battery_Power_Reserve"] = 4.0
    dummy_rest.rest_data["Control"]["Battery_Charge_Rate"] = 1100
    dummy_rest.rest_data["Control"]["Battery_Discharge_Rate"] = 1500
    dummy_rest.rest_data["Control"]["Enable_Charge_Schedule"] = "enable" if expect_charge_enable else "disable"
    dummy_rest.rest_data["Control"]["Enable_Discharge_Schedule"] = "enable" if expect_discharge_enable else "disable"
    dummy_rest.rest_data["Timeslots"] = {}
    dummy_rest.rest_data["Timeslots"]["Charge_start_time_slot_1"] = expect_charge_start_time
    dummy_rest.rest_data["Timeslots"]["Charge_end_time_slot_1"] = expect_charge_end_time
    dummy_rest.rest_data["Timeslots"]["Discharge_start_time_slot_1"] = expect_discharge_start_time
    dummy_rest.rest_data["Timeslots"]["Discharge_end_time_slot_1"] = expect_discharge_end_time
    dummy_rest.rest_data["Power"] = {}
    dummy_rest.rest_data["Power"]["Power"] = {}
    dummy_rest.rest_data["Power"]["Power"]["SOC_kWh"] = expect_soc_kwh
    dummy_rest.rest_data["Power"]["Power"]["Battery_Power"] = expect_battery_power
    dummy_rest.rest_data["Power"]["Power"]["PV_Power"] = expect_pv_power
    dummy_rest.rest_data["Power"]["Power"]["Load_Power"] = expect_load_power
    dummy_rest.rest_data["Invertor_Details"] = {}
    dummy_rest.rest_data["Invertor_Details"]["Battery_Capacity_kWh"] = expect_battery_capacity
    dummy_rest.rest_data["raw"] = {}
    dummy_rest.rest_data["raw"]["invertor"] = {}
    dummy_rest.rest_data["raw"]["invertor"]["discharge_target_soc_1"] = 4
    dummy_items["sensor.soc_kw"] = -1
    dummy_items["sensor.battery_capacity"] = -1

    inv = Inverter(my_predbat, 0, rest_postCommand=dummy_rest.dummy_rest_postCommand, rest_getData=dummy_rest.dummy_rest_getData)
    inv.sleep = dummy_sleep

    print("Test: Update Inverter - REST")
    inv.update_status(my_predbat.minutes_now)

    if inv.charge_start_time_minutes != charge_start_time_minutes:
        print("ERROR: Charge start time should be {} got {} ({})".format(charge_start_time_minutes, inv.charge_start_time_minutes, dummy_items["select.charge_start_time"]))
        failed = True
    if inv.charge_end_time_minutes != charge_end_time_minutes:
        print("ERROR: Charge end time should be {} got {}".format(charge_end_time_minutes, inv.charge_end_time_minutes))
        failed = True
    if inv.charge_enable_time != expect_charge_enable:
        print("ERROR: Charge enable should be {} got {}".format(expect_charge_enable, inv.charge_enable_time))
        failed = True
    if inv.discharge_start_time_minutes != discharge_start_time_minutes:
        print("ERROR: Discharge start time should be {} got {}".format(discharge_start_time_minutes, inv.discharge_start_time_minutes))
        failed = True
    if inv.discharge_end_time_minutes != discharge_end_time_minutes:
        print("ERROR: Discharge end time should be {} got {}".format(discharge_end_time_minutes, inv.discharge_end_time_minutes))
        failed = True
    if inv.discharge_enable_time != expect_discharge_enable:
        print("ERROR: Discharge enable should be {} got {}".format(expect_discharge_enable, inv.discharge_enable_time))
        failed = True
    if inv.battery_power != expect_battery_power:
        print("ERROR: Battery power should be {} got {}".format(expect_battery_power, inv.battery_power))
        failed = True
    if inv.pv_power != expect_pv_power:
        print("ERROR: PV power should be {} got {}".format(expect_pv_power, inv.pv_power))
        failed = True
    if inv.load_power != expect_load_power:
        print("ERROR: Load power should be {} got {}".format(expect_load_power, inv.load_power))
        failed = True
    if inv.soc_kw != expect_soc_kwh:
        print("ERROR: SOC kWh should be {} got {}".format(expect_soc_kwh, inv.soc_kw))
        failed = True
    if inv.soc_max != expect_battery_capacity:
        print("ERROR: SOC Max should be {} got {}".format(expect_battery_capacity, inv.soc_max))
        failed = True

    my_predbat.args["soc_kw"] = "sensor.soc_kw"

    return failed


def test_auto_restart(test_name, my_predbat, ha, inv, dummy_items, service, expected, active=False):
    print("**** Running Test: {} ****".format(test_name))
    failed = 0
    ha.service_store_enable = True
    ha.service_store = []
    my_predbat.restart_active = active

    my_predbat.args["auto_restart"] = service

    failed = 1 if not active else 0
    try:
        inv.auto_restart("Crashed")
    except Exception as e:
        failed = 0
        if str(e) != "Auto-restart triggered":
            print("ERROR: Auto-restart should be triggered got {}".format(e))
            failed = 1

    result = ha.get_service_store()
    if json.dumps(expected) != json.dumps(result):
        print("ERROR: Auto-restart service should be {} got {}".format(expected, result))
        failed = 1
    return failed


def test_call_adjust_charge_immediate(test_name, my_predbat, ha, inv, dummy_items, soc, repeat=False, freeze=False, clear=False, stop_discharge=False, charge_start_time="00:00:00", charge_end_time="23:55:00", no_freeze=False):
    """
    Tests;
        def adjust_charge_immediate(self, target_soc, freeze=False)
    """
    failed = False
    ha.service_store_enable = True
    if clear:
        ha.service_store = []

    print("**** Running Test: {} ****".format(test_name))

    my_predbat.args["charge_start_service"] = "charge_start"
    my_predbat.args["charge_stop_service"] = "charge_stop"
    if not no_freeze:
        my_predbat.args["charge_freeze_service"] = "charge_freeze"
    else:
        my_predbat.args["charge_freeze_service"] = None
    my_predbat.args["discharge_start_service"] = "discharge_start"
    my_predbat.args["discharge_stop_service"] = "discharge_stop"
    my_predbat.args["discharge_freeze_service"] = "discharge_freeze"
    my_predbat.args["charge_rate"] = "number.charge_rate"
    my_predbat.args["device_id"] = "DID0"
    if "charge_rate_percent" in my_predbat.args:
        del my_predbat.args["charge_rate_percent"]

    dummy_items["select.charge_start_time"] = charge_start_time
    dummy_items["select.charge_end_time"] = charge_end_time
    dummy_items["number.charge_rate"] = 1101

    power = 1101

    inv.adjust_charge_immediate(soc, freeze=freeze)
    result = ha.get_service_store()
    expected = []

    if repeat:
        pass
    elif soc == inv.soc_percent or freeze:
        if stop_discharge:
            expected.append(["discharge_stop", {"device_id": "DID0"}])
        expected.append(["charge_freeze", {"device_id": "DID0", "target_soc": soc, "power": power}])
    elif soc > 0:
        if stop_discharge:
            expected.append(["discharge_stop", {"device_id": "DID0"}])
        expected.append(["charge_start", {"device_id": "DID0", "target_soc": soc, "power": power}])
    else:
        expected.append(["charge_stop", {"device_id": "DID0"}])
    if json.dumps(expected) != json.dumps(result):
        print("ERROR: Adjust charge immediate - charge service should be {} got {}".format(expected, result))
        failed = True

    ha.service_store_enable = False
    return failed


def test_call_adjust_export_immediate(test_name, my_predbat, ha, inv, dummy_items, soc, repeat=False, freeze=False, clear=False, charge_stop=False, discharge_start_time="00:00:00", discharge_end_time="23:55:00", no_freeze=False):
    """
    Tests;
        def adjust_export_immediate(self, target_soc, freeze=False)
    """
    failed = False
    ha.service_store_enable = True
    if clear:
        ha.service_store = []

    print("**** Running Test: {} ****".format(test_name))

    my_predbat.args["charge_start_service"] = "charge_start"
    my_predbat.args["charge_stop_service"] = "charge_stop"
    my_predbat.args["charge_freeze_service"] = "charge_freeze"
    my_predbat.args["discharge_start_service"] = "discharge_start"
    my_predbat.args["discharge_stop_service"] = "discharge_stop"
    if not no_freeze:
        my_predbat.args["discharge_freeze_service"] = "discharge_freeze"
    else:
        my_predbat.args["discharge_freeze_service"] = None
    my_predbat.args["device_id"] = "DID0"
    power = int(inv.battery_rate_max_discharge * MINUTE_WATT)

    dummy_items["select.discharge_start_time"] = discharge_start_time
    dummy_items["select.discharge_end_time"] = discharge_end_time

    inv.adjust_export_immediate(soc, freeze=freeze)
    result = ha.get_service_store()
    expected = []

    if repeat:
        pass
    elif freeze:
        if charge_stop:
            expected.append(["charge_stop", {"device_id": "DID0"}])
        expected.append(["discharge_freeze", {"device_id": "DID0", "target_soc": soc, "power": power}])
    elif soc > 0 and soc < 100:
        if charge_stop:
            expected.append(["charge_stop", {"device_id": "DID0"}])
        expected.append(["discharge_start", {"device_id": "DID0", "target_soc": soc, "power": power}])
    else:
        expected.append(["discharge_stop", {"device_id": "DID0"}])
    if json.dumps(expected) != json.dumps(result):
        print("ERROR: Adjust export immediate - discharge service should be {} got {}".format(expected, result))
        failed = True

    ha.service_store_enable = False
    return failed


def test_call_service_template(test_name, my_predbat, inv, service_name="test", domain="charge", data={}, extra_data={}, clear=True, repeat=False, service_template=None, expected_result=None, twice=True):
    """
    tests
        def call_service_template(self, service, data, domain="charge", extra_data={})
    """
    failed = False

    print("**** Running Test: {} ****".format(test_name))

    ha = my_predbat.ha_interface
    ha.service_store_enable = True
    service_call = service_name + "_service"

    if service_template:
        my_predbat.args[service_name] = service_template
    else:
        my_predbat.args[service_name] = service_call

    if clear:
        my_predbat.last_service_hash = {}

    inv.call_service_template(service_name, data, domain=domain, extra_data=extra_data)
    if repeat:
        expected = []
    else:
        expected = [[service_call, data]] if (expected_result is None) else expected_result

    result = ha.get_service_store()
    if json.dumps(expected) != json.dumps(result):
        print("ERROR: {} service should be {} got {} - 1".format(service_name, json.dumps(expected), json.dumps(result)))
        failed = True

    if twice:
        inv.call_service_template(service_name, data, domain=domain, extra_data=extra_data)
        expected = []
        result = ha.get_service_store()
        if json.dumps(expected) != json.dumps(result):
            print("ERROR: {} service should be {} got {} - 2".format(service_name, expected, result))
            failed = True

    ha.service_store_enable = False
    return failed


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
        print(slots)
    total_pd = my_predbat.car_charge_slot_kwh(my_predbat.minutes_now, my_predbat.minutes_now + my_predbat.forecast_minutes)
    if total_pd != expect_kwh:
        print("ERROR: Car charging total calculated with car_charge_slot_kwh should be {} got {}".format(expect_kwh, total_pd))
        failed = True
        print(slots)
    if total_cost != expect_cost:
        print("ERROR: Car charging total cost should be {} got {}".format(expect_cost, total_cost))
        failed = True
        print(slots)

    return failed


def run_load_octopus_slot_test(testname, my_predbat, slots, expected_slots, consider_full, car_soc, car_limit, car_loss):
    """
    Run a test for load_octopus_slot
    octopus_slots = load_octopus_slots(self, octopus_slots, octopus_intelligent_consider_full)
    """
    failed = False
    print("**** Running Test: load_octopus_slot {} ****".format(testname))
    my_predbat.octopus_slots = slots
    my_predbat.octopus_intelligent_consider_full = consider_full
    my_predbat.car_charging_soc[0] = car_soc
    my_predbat.car_charging_limit[0] = car_limit
    my_predbat.car_charging_loss = car_loss

    result = my_predbat.load_octopus_slots(slots, consider_full)
    if json.dumps(result) != json.dumps(expected_slots):
        print("ERROR: Slots should be:\n ref: {}\n  got: {}".format(expected_slots, result))
        failed = True
    return failed


def assert_rates(rates, start_minute, end_minute, expect_rate):
    """
    Assert rates
    """
    end_minute = min(end_minute, len(rates))
    for minute in range(start_minute, end_minute):
        if rates[minute] != expect_rate:
            print("ERROR: Rate at minute {} should be {} got {}".format(minute, expect_rate, rates[minute]))
            results_short = {}
            for i in range(0, 48 * 60, 30):
                results_short[i] = rates[i]
            print("Rates: {}".format(results_short))
            return 1
    return 0


def test_basic_rates(my_predbat):
    """
    Test for basic rates function

    rates = basic_rates(self, info, rtype, prev=None, rate_replicate={}):
    """
    failed = 0

    old_midnight = my_predbat.midnight
    my_predbat.midnight = datetime.strptime("2025-07-05T00:00:00", "%Y-%m-%dT%H:%M:%S")

    print("*** Running test: Simple rate1")
    simple_rate = [
        {"rate": 5},
        {
            "rate": 10,
            "start": "17:00:00",
            "end": "19:00:00",
        },
    ]
    results = my_predbat.basic_rates(simple_rate, "import")
    results, results_replicated = my_predbat.rate_replicate(results, is_import=True, is_gas=False)

    failed |= assert_rates(results, 0, 17 * 60, 5)
    failed |= assert_rates(results, 17 * 60, 19 * 60, 10)
    failed |= assert_rates(results, 19 * 60, 24 * 60 + 17 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 17 * 60, 24 * 60 + 19 * 60, 10)

    print("*** Running test: Simple rate2")
    simple_rate2 = [{"rate": 5}, {"rate": 10, "start": "17:00:00", "end": "19:00:00", "day_of_week": 7}, {"rate": 9, "start": "17:00:00", "end": "19:00:00", "day_of_week": "5,6"}]
    results = my_predbat.basic_rates(simple_rate2, "import")
    results, results_replicated = my_predbat.rate_replicate(results, is_import=True, is_gas=False)

    failed |= assert_rates(results, 0, 17 * 60, 5)
    failed |= assert_rates(results, 17 * 60, 19 * 60, 9)
    failed |= assert_rates(results, 19 * 60, 24 * 60 + 17 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 17 * 60, 24 * 60 + 19 * 60, 10)

    print("*** Running test: Simple rate3")
    simple_rate3 = [
        {"rate": 10, "start": "01:00:00", "end": "17:00:00"},
        {
            "rate": 5,
            "start": "17:00:00",
            "end": "01:00:00",
        },
    ]
    results = my_predbat.basic_rates(simple_rate3, "import")
    results, results_replicated = my_predbat.rate_replicate(results, is_import=True, is_gas=False)
    failed |= assert_rates(results, 0, 1 * 60, 5)
    failed |= assert_rates(results, 1 * 60, 17 * 60, 10)
    failed |= assert_rates(results, 17 * 60, 25 * 60, 5)
    failed |= assert_rates(results, 25 * 60, 17 * 60 + 24 * 60, 10)
    failed |= assert_rates(results, 17 * 60 + 24 * 60, 48 * 60, 5)

    print("*** Running test: Simple rate4")
    rate_override = [{"start": "12:00:00", "end": "13:00:00", "rate_increment": 1}]
    results = my_predbat.basic_rates(simple_rate2, "import")
    results = my_predbat.basic_rates(rate_override, "import", prev=results)
    failed |= assert_rates(results, 0, 12 * 60, 5)
    failed |= assert_rates(results, 12 * 60, 13 * 60, 6)
    failed |= assert_rates(results, 13 * 60, 17 * 60, 5)
    failed |= assert_rates(results, 17 * 60, 19 * 60, 9)
    failed |= assert_rates(results, 19 * 60, 24 * 60 + 12 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 12 * 60, 24 * 60 + 13 * 60, 6)
    failed |= assert_rates(results, 24 * 60 + 13 * 60, 24 * 60 + 17 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 17 * 60, 24 * 60 + 19 * 60, 10)

    print("*** Running test: Simple rate5")
    rate_override = [{"start": "12:00:00", "end": "13:00:00", "rate_increment": 1, "date": my_predbat.midnight.strftime("%Y-%m-%d")}]
    print(rate_override)
    results = my_predbat.basic_rates(simple_rate2, "import")
    results = my_predbat.basic_rates(rate_override, "import", prev=results)
    failed |= assert_rates(results, 0, 12 * 60, 5)
    failed |= assert_rates(results, 12 * 60, 13 * 60, 6)
    failed |= assert_rates(results, 13 * 60, 17 * 60, 5)
    failed |= assert_rates(results, 17 * 60, 19 * 60, 9)
    failed |= assert_rates(results, 19 * 60, 24 * 60 + 12 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 12 * 60, 24 * 60 + 13 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 13 * 60, 24 * 60 + 17 * 60, 5)
    failed |= assert_rates(results, 24 * 60 + 17 * 60, 24 * 60 + 19 * 60, 10)

    my_predbat.midnight = old_midnight
    return failed


def run_load_octopus_slots_tests(my_predbat):
    """
    Test for load octopus slots


    slots are in format:

    - start: '2025-01-30T00:00:00+00:00'
      end: '2025-01-30T00:30:00+00:00'
      charge_in_kwh: -2.56
      source: null
      location: AT_HOME

    """
    failed = 0

    TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

    slots = []
    slots2 = []
    expected_slots = []
    expected_slots2 = []
    expected_slots3 = []
    expected_slots4 = []
    now_utc = my_predbat.now_utc
    now_utc = now_utc.replace(minute=0, second=0, microsecond=0)
    midnight_utc = my_predbat.midnight_utc

    reset_rates(my_predbat, 10, 5)
    my_predbat.rate_min = 4

    # Created 8 slots in total in the next 16 hours
    soc = 2.0
    soc2 = 2.0
    for i in range(8):
        start = now_utc + timedelta(minutes=i * 60)
        start_plus_15 = start + timedelta(minutes=15)
        end = start + timedelta(minutes=60)
        prev_soc = soc
        prev_soc2 = soc2
        soc += 5
        soc2 += 2.5
        slots.append({"start": start.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "charge_in_kwh": -5, "source": "null", "location": "AT_HOME"})
        slots2.append({"start": start.strftime(TIME_FORMAT) if i >= 1 else start_plus_15.strftime(TIME_FORMAT), "end": end.strftime(TIME_FORMAT), "charge_in_kwh": -5, "source": "null", "location": "AT_HOME"})
        minutes_start = int((start - midnight_utc).total_seconds() / 60)
        minutes_end = int((end - midnight_utc).total_seconds() / 60)
        expected_slots.append({"start": minutes_start, "end": minutes_end, "kwh": 5.0, "average": 4, "cost": 20.0, "soc": 0.0})
        expected_slots2.append({"start": minutes_start, "end": minutes_end, "kwh": 0.0, "average": 4, "cost": 0.0, "soc": 0.0})
        expected_slots3.append({"start": minutes_start, "end": minutes_end, "kwh": 5.0 if soc <= 12.0 else 0.0, "average": 4, "cost": 20.0 if soc <= 12.0 else 0.0, "soc": min(soc, 12.0)})
        if prev_soc2 < 10.0 and soc2 >= 10.0:
            expected_slots4.append({"start": minutes_start, "end": minutes_start + 30, "kwh": 1.0, "average": 4, "cost": 1 * 4.0, "soc": min(soc2, 10.0)})
            expected_slots4.append({"start": minutes_start + 30, "end": minutes_end, "kwh": 5.0 if soc <= 20.0 else 0.0, "average": 4, "cost": 20.0 if soc <= 20.0 else 0.0, "soc": min(soc2, 10.0)})
        else:
            expected_slots4.append({"start": minutes_start, "end": minutes_end, "kwh": 5.0 if soc <= 20.0 else 0.0, "average": 4, "cost": 20.0 if soc <= 20.0 else 0.0, "soc": min(soc2, 10.0)})

    failed |= run_load_octopus_slot_test("test1", my_predbat, slots, expected_slots, False, 2.0, 0.0, 1.0)

    # Misalign the start time by 15 minutes
    expected_slots[0]["start"] += 15
    failed |= run_load_octopus_slot_test("test1b", my_predbat, slots2, expected_slots, False, 2.0, 0.0, 1.0)

    failed |= run_load_octopus_slot_test("test2", my_predbat, slots, expected_slots2, True, 2.0, 0.0, 1.0)
    failed |= run_load_octopus_slot_test("test3", my_predbat, slots, expected_slots3, True, 2.0, 12.0, 1.0)
    failed |= run_load_octopus_slot_test("test4", my_predbat, slots, expected_slots4, True, 2.0, 10.0, 0.5)
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


def test_inverter_self_test(test_name, my_predbat):
    failed = 0

    print("**** Running Test: {} ****".format(test_name))
    # Call self test - doesn't really check much as such except the code isn't dead
    dummy_rest = DummyRestAPI()
    my_predbat.args["givtcp_rest"] = "dummy"

    dummy_rest.rest_data = {}
    dummy_rest.rest_data["Control"] = {}
    dummy_rest.rest_data["Control"]["Target_SOC"] = 99
    dummy_rest.rest_data["Control"]["Mode"] = "Eco"
    dummy_rest.rest_data["Control"]["Battery_Power_Reserve"] = 4.0
    dummy_rest.rest_data["Control"]["Battery_Charge_Rate"] = 1100
    dummy_rest.rest_data["Control"]["Battery_Discharge_Rate"] = 1500
    dummy_rest.rest_data["Control"]["Enable_Charge_Schedule"] = "enable"
    dummy_rest.rest_data["Control"]["Enable_Discharge_Schedule"] = "enable"
    dummy_rest.rest_data["Timeslots"] = {}
    dummy_rest.rest_data["Timeslots"]["Charge_start_time_slot_1"] = "00:30:00"
    dummy_rest.rest_data["Timeslots"]["Charge_end_time_slot_1"] = "22:00:00"
    dummy_rest.rest_data["Timeslots"]["Discharge_start_time_slot_1"] = "01:00:00"
    dummy_rest.rest_data["Timeslots"]["Discharge_end_time_slot_1"] = "02:30:00"
    dummy_rest.rest_data["Power"] = {}
    dummy_rest.rest_data["Power"]["Power"] = {}
    dummy_rest.rest_data["Power"]["Power"]["SOC_kWh"] = 1.0
    dummy_rest.rest_data["Power"]["Power"]["Battery_Power"] = 100
    dummy_rest.rest_data["Power"]["Power"]["PV_Power"] = 200
    dummy_rest.rest_data["Power"]["Power"]["Load_Power"] = 300

    inv = Inverter(my_predbat, 0, rest_postCommand=dummy_rest.dummy_rest_postCommand, rest_getData=dummy_rest.dummy_rest_getData)
    inv.sleep = dummy_sleep
    inv.self_test(my_predbat.minutes_now)
    rest = dummy_rest.get_commands()
    expected = [
        ["dummy/setChargeTarget", {"chargeToPercent": 100}],
        ["dummy/setChargeTarget", {"chargeToPercent": 100}],
        ["dummy/setChargeTarget", {"chargeToPercent": 100}],
        ["dummy/setChargeTarget", {"chargeToPercent": 100}],
        ["dummy/setChargeTarget", {"chargeToPercent": 100}],
        ["dummy/setChargeRate", {"chargeRate": 215}],
        ["dummy/setChargeRate", {"chargeRate": 215}],
        ["dummy/setChargeRate", {"chargeRate": 215}],
        ["dummy/setChargeRate", {"chargeRate": 215}],
        ["dummy/setChargeRate", {"chargeRate": 215}],
        ["dummy/setChargeRate", {"chargeRate": 0}],
        ["dummy/setChargeRate", {"chargeRate": 0}],
        ["dummy/setChargeRate", {"chargeRate": 0}],
        ["dummy/setChargeRate", {"chargeRate": 0}],
        ["dummy/setChargeRate", {"chargeRate": 0}],
        ["dummy/setDischargeRate", {"dischargeRate": 220}],
        ["dummy/setDischargeRate", {"dischargeRate": 220}],
        ["dummy/setDischargeRate", {"dischargeRate": 220}],
        ["dummy/setDischargeRate", {"dischargeRate": 220}],
        ["dummy/setDischargeRate", {"dischargeRate": 220}],
        ["dummy/setDischargeRate", {"dischargeRate": 0}],
        ["dummy/setDischargeRate", {"dischargeRate": 0}],
        ["dummy/setDischargeRate", {"dischargeRate": 0}],
        ["dummy/setDischargeRate", {"dischargeRate": 0}],
        ["dummy/setDischargeRate", {"dischargeRate": 0}],
        ["dummy/setBatteryReserve", {"reservePercent": 100}],
        ["dummy/setBatteryReserve", {"reservePercent": 100}],
        ["dummy/setBatteryReserve", {"reservePercent": 100}],
        ["dummy/setBatteryReserve", {"reservePercent": 100}],
        ["dummy/setBatteryReserve", {"reservePercent": 100}],
        ["dummy/setBatteryReserve", {"reservePercent": 6}],
        ["dummy/setBatteryReserve", {"reservePercent": 6}],
        ["dummy/setBatteryReserve", {"reservePercent": 6}],
        ["dummy/setBatteryReserve", {"reservePercent": 6}],
        ["dummy/setBatteryReserve", {"reservePercent": 6}],
        ["dummy/enableChargeSchedule", {"state": "disable"}],
        ["dummy/enableChargeSchedule", {"state": "disable"}],
        ["dummy/enableChargeSchedule", {"state": "disable"}],
        ["dummy/enableChargeSchedule", {"state": "disable"}],
        ["dummy/enableChargeSchedule", {"state": "disable"}],
        ["dummy/setChargeSlot1", {"start": "23:01", "finish": "05:01"}],
        ["dummy/setChargeSlot1", {"start": "23:01", "finish": "05:01"}],
        ["dummy/setChargeSlot1", {"start": "23:01", "finish": "05:01"}],
        ["dummy/setChargeSlot1", {"start": "23:01", "finish": "05:01"}],
        ["dummy/setChargeSlot1", {"start": "23:01", "finish": "05:01"}],
        ["dummy/setChargeSlot1", {"start": "23:00", "finish": "05:00"}],
        ["dummy/setChargeSlot1", {"start": "23:00", "finish": "05:00"}],
        ["dummy/setChargeSlot1", {"start": "23:00", "finish": "05:00"}],
        ["dummy/setChargeSlot1", {"start": "23:00", "finish": "05:00"}],
        ["dummy/setChargeSlot1", {"start": "23:00", "finish": "05:00"}],
        ["dummy/setDischargeSlot1", {"start": "23:00", "finish": "23:01"}],
        ["dummy/setDischargeSlot1", {"start": "23:00", "finish": "23:01"}],
        ["dummy/setDischargeSlot1", {"start": "23:00", "finish": "23:01"}],
        ["dummy/setDischargeSlot1", {"start": "23:00", "finish": "23:01"}],
        ["dummy/setDischargeSlot1", {"start": "23:00", "finish": "23:01"}],
        ["dummy/setBatteryMode", {"mode": "Timed Export"}],
        ["dummy/setBatteryMode", {"mode": "Timed Export"}],
        ["dummy/setBatteryMode", {"mode": "Timed Export"}],
        ["dummy/setBatteryMode", {"mode": "Timed Export"}],
        ["dummy/setBatteryMode", {"mode": "Timed Export"}],
    ]
    if json.dumps(expected) != json.dumps(rest):
        print("ERROR: Self test should be {} got {}".format(expected, rest))
        failed = True
    return failed


def run_inverter_tests():
    """
    Test the inverter functions
    """
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

    failed = False
    print("**** Running Inverter tests ****")
    ha = my_predbat.ha_interface

    time_now = my_predbat.now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
    dummy_items = {
        "number.charge_rate": 1100,
        "number.discharge_rate": 1500,
        "number.charge_rate_percent": 100,
        "number.discharge_rate_percent": 100,
        "number.charge_limit": 100,
        "select.pause_mode": "Disabled",
        "sensor.battery_capacity": 10.0,
        "sensor.battery_soc": 0.0,
        "sensor.soc_max": 10.0,
        "sensor.soc_kw": 1.0,
        "select.inverter_mode": "Eco",
        "sensor.inverter_time": time_now,
        "switch.restart": False,
        "select.idle_start_time": "00:00",
        "select.idle_end_time": "00:00",
        "sensor.battery_power": 5.0,
        "sensor.pv_power": 1.0,
        "sensor.load_power": 2.0,
        "number.reserve": 4.0,
        "switch.scheduled_charge_enable": "off",
        "switch.scheduled_discharge_enable": "off",
        "select.charge_start_time": "01:11:00",
        "select.charge_end_time": "02:22:00",
        "select.discharge_start_time": "03:33:00",
        "select.discharge_end_time": "04:44:00",
        "sensor.predbat_GE_0_scheduled_discharge_enable": "off",
        "number.discharge_target_soc": 4,
    }
    my_predbat.ha_interface.dummy_items = dummy_items
    my_predbat.args["auto_restart"] = [{"service": "switch/turn_on", "entity_id": "switch.restart"}]
    my_predbat.args["givtcp_rest"] = None
    my_predbat.args["inverter_type"] = ["GE"]
    for entity_id in dummy_items.keys():
        arg_name = entity_id.split(".")[1]
        my_predbat.args[arg_name] = entity_id

    failed |= test_inverter_update(
        "update1",
        my_predbat,
        dummy_items,
        expect_charge_start_time="01:11:00",
        expect_charge_end_time="02:22:00",
        expect_charge_enable=False,
        expect_discharge_start_time="03:33:00",
        expect_discharge_end_time="04:44:00",
        expect_discharge_enable=True,
        expect_battery_power=5.0,
        expect_pv_power=1.0,
        expect_load_power=2.0,
        expect_soc_kwh=6.0,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_update(
        "update1b",
        my_predbat,
        dummy_items,
        expect_charge_start_time="01:11:00",
        expect_charge_end_time="02:22:00",
        expect_charge_enable=True,
        expect_discharge_start_time="03:33:00",
        expect_discharge_end_time="04:44:00",
        expect_discharge_enable=True,
        expect_battery_power=5.0,
        expect_pv_power=1.0,
        expect_load_power=2.0,
        expect_soc_kwh=6.0,
        has_charge_enable_time=False,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_update(
        "update1c",
        my_predbat,
        dummy_items,
        expect_charge_start_time="02:11:00",
        expect_charge_end_time="01:22:00",
        expect_charge_enable=True,
        expect_discharge_start_time="04:33:00",
        expect_discharge_end_time="03:44:00",
        expect_discharge_enable=True,
        expect_battery_power=5.0,
        expect_pv_power=1.0,
        expect_load_power=2.0,
        expect_soc_kwh=6.0,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_update(
        "update2",
        my_predbat,
        dummy_items,
        expect_charge_start_time="01:11:00",
        expect_charge_end_time="23:22:00",
        expect_charge_enable=True,
        expect_discharge_start_time="03:33:00",
        expect_discharge_end_time="04:44:00",
        expect_discharge_enable=True,
        expect_battery_power=6.0,
        expect_pv_power=1.5,
        expect_load_power=2.5,
        expect_soc_kwh=6.6,
        soc_percent=True,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_update(
        "update3",
        my_predbat,
        dummy_items,
        expect_charge_start_time="01:11:00",
        expect_charge_end_time="23:22:00",
        expect_charge_enable=True,
        expect_discharge_start_time="03:33:00",
        expect_discharge_end_time="04:44:00",
        expect_discharge_enable=True,
        expect_battery_power=6.0,
        expect_pv_power=1.5,
        expect_load_power=2.5,
        expect_soc_kwh=6.6,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_update(
        "update4a",
        my_predbat,
        dummy_items,
        expect_charge_start_time="01:11:00",
        expect_charge_end_time="01:11:00",
        expect_charge_enable=True,
        expect_discharge_start_time="03:33:00",
        expect_discharge_end_time="03:33:00",
        expect_discharge_enable=False,
        expect_battery_power=6.0,
        expect_pv_power=1.5,
        expect_load_power=2.5,
        expect_soc_kwh=6.6,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_update(
        "update4b",
        my_predbat,
        dummy_items,
        expect_charge_start_time="01:11:00",
        expect_charge_end_time="01:11:00",
        expect_charge_enable=False,
        expect_discharge_start_time="03:33:00",
        expect_discharge_end_time="03:33:00",
        expect_discharge_enable=False,
        expect_battery_power=6.0,
        expect_pv_power=1.5,
        expect_load_power=2.5,
        expect_soc_kwh=6.6,
        has_charge_enable_time=False,
        has_discharge_enable_time=False,
    )
    if failed:
        return failed

    failed |= test_inverter_rest_template(
        "rest1",
        my_predbat,
        filename="cases/rest_v2.json",
        assert_soc_max=9.523,
        assert_soc=3.333,
        assert_pv_power=10,
        assert_load_power=624,
        assert_charge_start_time_minutes=1410,
        assert_charge_end_time_minutes=1770,
        assert_discharge_start_time_minutes=1380,
        assert_discharge_end_time_minutes=1441,
        assert_discharge_enable=False,
        assert_charge_enable=True,
        assert_nominal_capacity=9.5232,
        assert_battery_temperature=15.3,
    )
    if failed:
        return failed
    failed |= test_inverter_rest_template(
        "rest2",
        my_predbat,
        filename="cases/rest_v3.json",
        assert_voltage=53.65,
        assert_battery_rate_max=3600,
        assert_serial_number="EA2303G082",
        assert_soc=7.62,
        assert_pv_power=247.0,
        assert_load_power=197.0,
        assert_charge_start_time_minutes=1440,
        assert_charge_end_time_minutes=1440,
        assert_discharge_start_time_minutes=1445,
        assert_discharge_end_time_minutes=1531,
        assert_discharge_enable=True,
        assert_nominal_capacity=9.52,
        assert_battery_temperature=25.0,
    )
    if failed:
        return failed

    my_predbat.args["givtcp_rest"] = None
    dummy_rest = DummyRestAPI()
    inv = Inverter(my_predbat, 0, rest_postCommand=dummy_rest.dummy_rest_postCommand, rest_getData=dummy_rest.dummy_rest_getData)
    inv.sleep = dummy_sleep
    inv.update_status(my_predbat.minutes_now)
    my_predbat.inv = inv

    failed |= test_adjust_force_export("adjust_force_export1", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, 4, "11:00:00", "11:30:00", False)
    if failed:
        return failed
    failed |= test_adjust_force_export("adjust_force_export2", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, 4, "11:00:00", "11:30:00", True)
    if failed:
        return failed
    failed |= test_adjust_force_export("adjust_force_export3", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, 10, "11:00:00", "11:30:00", True)
    if failed:
        return failed
    failed |= test_adjust_force_export("adjust_force_export4", ha, inv, dummy_rest, "00:11:00", "01:12:12", True, 10, "11:00:00", "11:30:00", True)
    if failed:
        return failed
    failed |= test_adjust_force_export("adjust_force_export5", ha, inv, dummy_rest, "00:11:00", "01:12:12", True, 4, "11:00:00", "11:30:00", False)
    if failed:
        return failed

    failed |= test_adjust_battery_target("adjust_target50", ha, inv, dummy_rest, 0, 50, True, False, 50)
    failed |= test_adjust_battery_target("adjust_target0", ha, inv, dummy_rest, 10, 0, True, False, 4)
    failed |= test_adjust_battery_target("adjust_target100", ha, inv, dummy_rest, 99, 100, True, False, 100)
    failed |= test_adjust_battery_target("adjust_target100r", ha, inv, dummy_rest, 100, 100, True, False, 100)
    if failed:
        return failed

    failed |= test_adjust_inverter_mode("adjust_mode_eco1", ha, inv, dummy_rest, "Timed Export", "Eco", "Eco")
    failed |= test_adjust_inverter_mode("adjust_mode_eco2", ha, inv, dummy_rest, "Eco", "Eco", "Eco")
    failed |= test_adjust_inverter_mode("adjust_mode_eco3", ha, inv, dummy_rest, "Eco (Paused)", "Eco", "Eco (Paused)")
    failed |= test_adjust_inverter_mode("adjust_mode_export1", ha, inv, dummy_rest, "Eco (Paused)", "Timed Export", "Timed Export")
    failed |= test_adjust_inverter_mode("adjust_mode_export2", ha, inv, dummy_rest, "Timed Export", "Timed Export", "Timed Export")
    if failed:
        return failed

    failed |= test_adjust_charge_rate("adjust_charge_rate1", ha, inv, dummy_rest, 0, 200.1, 200)
    if failed:
        return failed
    failed |= test_adjust_charge_rate("adjust_charge_rate2", ha, inv, dummy_rest, 0, 100, 0)
    if failed:
        return failed
    failed |= test_adjust_charge_rate("adjust_charge_rate3", ha, inv, dummy_rest, 200, 0, 0)
    failed |= test_adjust_charge_rate("adjust_charge_rate4", ha, inv, dummy_rest, 100, 0, 100)
    failed |= test_adjust_charge_rate("adjust_charge_rate5", ha, inv, dummy_rest, 200, 210, 200)
    if failed:
        return failed

    failed |= test_adjust_charge_rate("adjust_discharge_rate1", ha, inv, dummy_rest, 0, 250.1, 250, discharge=True)
    failed |= test_adjust_charge_rate("adjust_discharge_rate2", ha, inv, dummy_rest, 250, 0, 0, discharge=True)
    failed |= test_adjust_charge_rate("adjust_discharge_rate3", ha, inv, dummy_rest, 200, 210, 200, discharge=True)
    if failed:
        return failed

    failed |= test_adjust_reserve("adjust_reserve1", ha, inv, dummy_rest, 4, 50, reserve_max=100)
    failed |= test_adjust_reserve("adjust_reserve2", ha, inv, dummy_rest, 50, 0, 4, reserve_max=100)
    failed |= test_adjust_reserve("adjust_reserve3", ha, inv, dummy_rest, 20, 100, reserve_max=100)
    failed |= test_adjust_reserve("adjust_reserve4", ha, inv, dummy_rest, 20, 100, 98, reserve_min=4, reserve_max=98)
    failed |= test_adjust_reserve("adjust_reserve5", ha, inv, dummy_rest, 50, 0, 0, reserve_min=0, reserve_max=100)
    if failed:
        return failed

    failed |= test_adjust_charge_window("adjust_charge_window1", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, "00:00:00", "00:00:00", my_predbat.minutes_now)
    failed |= test_adjust_charge_window("adjust_charge_window2", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, "00:00:00", "23:00:00", my_predbat.minutes_now)
    failed |= test_adjust_charge_window("adjust_charge_window3", ha, inv, dummy_rest, "00:00:00", "00:00:00", True, "00:00:00", "23:00:00", my_predbat.minutes_now)
    failed |= test_adjust_charge_window("adjust_charge_window4", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, "01:12:00", "23:12:00", my_predbat.minutes_now)
    failed |= test_adjust_charge_window("adjust_charge_window5", ha, inv, dummy_rest, "00:00:00", "00:00:00", True, "01:12:00", "23:12:00", my_predbat.minutes_now)
    failed |= test_adjust_charge_window("adjust_charge_window6", ha, inv, dummy_rest, "00:00:00", "00:00:00", True, "01:12:00", "23:12:00", my_predbat.minutes_now, short=True)
    if failed:
        return failed

    failed |= test_call_service_template("test_service_simple1", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra": "data"})
    failed |= test_call_service_template("test_service_simple2", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra": "data"}, clear=False, repeat=True)
    failed |= test_call_service_template("test_service_simple3", my_predbat, inv, service_name="test_service", domain="discharge", data={"test": "data"}, extra_data={"extra": "data"}, clear=False)
    failed |= test_call_service_template("test_service_simple4", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra": "data"}, clear=False, repeat=True)
    failed |= test_call_service_template("test_service_simple5", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra": "data2"}, clear=False, repeat=False)
    failed |= test_call_service_template("test_service_simple6", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra_dummy": "data2"}, clear=False, repeat=False)
    if failed:
        return failed

    failed |= test_call_service_template(
        "test_service_complex1",
        my_predbat,
        inv,
        service_name="complex_service",
        domain="charge",
        data={"test": "data"},
        extra_data={"extra": "extra_data"},
        service_template={"service": "funny", "dummy": "22", "extra": "{extra}"},
        expected_result=[["funny", {"dummy": "22", "extra": "extra_data"}]],
        clear=False,
    )
    failed |= test_call_service_template(
        "test_service_complex2", my_predbat, inv, service_name="complex_service", domain="charge", data={"test": "data"}, extra_data={"extra": "extra_data"}, service_template={"service": "funny", "dummy": "22", "extra": "{extra}"}, clear=False, repeat=True
    )
    failed |= test_call_service_template(
        "test_service_complex3",
        my_predbat,
        inv,
        service_name="complex_service",
        domain="charge",
        data={"test": "data"},
        extra_data={"extra": "extra_data"},
        service_template={"service": "funny", "dummy": "22", "extra": "{extra}", "always": True},
        expected_result=[["funny", {"dummy": "22", "extra": "extra_data"}]],
        clear=False,
        repeat=False,
        twice=False,
    )

    my_predbat.args["extra"] = "42"
    failed |= test_call_service_template(
        "test_service_complex4",
        my_predbat,
        inv,
        service_name="complex_service",
        domain="charge",
        data={"test": "data"},
        extra_data={"extra": "extra_data"},
        service_template={"service": "funny", "dummy": "22", "extra": "{extra}"},
        expected_result=[["funny", {"dummy": "22", "extra": "extra_data"}]],
        clear=True,
    )

    dummy_yaml = """
    service: select.select_option
    entity_id: "select.solaredge_i1_storage_command_mode"
    option: "Charge from Solar Power and Grid"
    always: true
    """
    decoded_yaml = yaml.safe_load(dummy_yaml)

    for repeat in range(2):
        failed |= test_call_service_template(
            "test_service_complex5",
            my_predbat,
            inv,
            service_name="charge_start_service",
            domain="charge",
            data={"test": "data"},
            extra_data={"extra": "extra_data"},
            service_template=decoded_yaml,
            expected_result=[["select/select_option", {"entity_id": "select.solaredge_i1_storage_command_mode", "option": "Charge from Solar Power and Grid"}]],
            clear=False,
            repeat=False,
            twice=False,
        )

    inv.soc_percent = 49

    if failed:
        return failed

    failed |= test_call_adjust_charge_immediate("charge_immediate1", my_predbat, ha, inv, dummy_items, 100, clear=True, stop_discharge=True)
    failed |= test_call_adjust_charge_immediate("charge_immediate2", my_predbat, ha, inv, dummy_items, 0)
    failed |= test_call_adjust_charge_immediate("charge_immediate3", my_predbat, ha, inv, dummy_items, 0, repeat=True)
    failed |= test_call_adjust_charge_immediate("charge_immediate4", my_predbat, ha, inv, dummy_items, 50)
    failed |= test_call_adjust_charge_immediate("charge_immediate5", my_predbat, ha, inv, dummy_items, 49)
    failed |= test_call_adjust_charge_immediate("charge_immediate6", my_predbat, ha, inv, dummy_items, 49, repeat=True)
    failed |= test_call_adjust_charge_immediate("charge_immediate6", my_predbat, ha, inv, dummy_items, 49, charge_start_time="00:00:00", charge_end_time="11:00:00")
    failed |= test_call_adjust_charge_immediate("charge_immediate7", my_predbat, ha, inv, dummy_items, 50, freeze=True)
    failed |= test_call_adjust_charge_immediate("charge_immediate8", my_predbat, ha, inv, dummy_items, 50, freeze=False, no_freeze=True)
    if failed:
        return failed

    failed |= test_call_adjust_export_immediate("export_immediate1", my_predbat, ha, inv, dummy_items, 100, repeat=True)
    failed |= test_call_adjust_export_immediate("export_immediate3", my_predbat, ha, inv, dummy_items, 0, repeat=True)
    failed |= test_call_adjust_export_immediate("export_immediate4", my_predbat, ha, inv, dummy_items, 50, charge_stop=True)
    failed |= test_call_adjust_export_immediate("export_immediate5", my_predbat, ha, inv, dummy_items, 49)
    failed |= test_call_adjust_export_immediate("export_immediate6", my_predbat, ha, inv, dummy_items, 49, repeat=True)
    failed |= test_call_adjust_export_immediate("export_immediate6", my_predbat, ha, inv, dummy_items, 49, discharge_start_time="00:00:00", discharge_end_time="09:00:00")
    failed |= test_call_adjust_export_immediate("export_immediate7", my_predbat, ha, inv, dummy_items, 50, freeze=True)
    failed |= test_call_adjust_export_immediate("export_immediate8", my_predbat, ha, inv, dummy_items, 50, freeze=False, no_freeze=True)
    if failed:
        return failed

    failed |= test_auto_restart(
        "auto_restart0",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service=None,
        expected=[],
        active=True,
    )
    if failed:
        return failed

    failed |= test_auto_restart(
        "auto_restart1",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service={"command": "service", "service": "restart_service", "addon": "adds"},
        expected=[["restart_service", {"addon": "adds"}], ["notify/notify", {"message": "Auto-restart service restart_service called due to: Crashed"}]],
    )
    if failed:
        return failed

    failed |= test_auto_restart(
        "auto_restart2", my_predbat, ha, inv, dummy_items, service=[{"command": "service", "service": "restart_service"}], expected=[["restart_service", {}], ["notify/notify", {"message": "Auto-restart service restart_service called due to: Crashed"}]]
    )
    if failed:
        return failed

    failed |= test_auto_restart(
        "auto_restart3",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service={"command": "service", "service": "restart_service"},
        expected=[],
        active=True,
    )
    if failed:
        return failed

    failed |= test_auto_restart(
        "auto_restart4",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service={"command": "service", "service": "restart_service", "entity_id": "switch.restart"},
        expected=[["restart_service", {"entity_id": "switch.restart"}], ["notify/notify", {"message": "Auto-restart service restart_service called due to: Crashed"}]],
    )
    if failed:
        return failed

    os.system("touch tmp1234")
    failed |= test_auto_restart(
        "auto_restart5",
        my_predbat,
        ha,
        inv,
        dummy_items,
        service={"command": "service", "shell": "rm tmp1234"},
        expected=[],
    )
    if failed:
        return failed
    if os.path.exists("tmp1234"):
        print("ERROR: File should be deleted")
        failed = True

    failed |= test_inverter_self_test("self_test1", my_predbat)
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
    iboost_smart_min_length=30,
    assert_final_iboost=0.0,
    end_record=None,
    pv10=False,
    carbon=0,
    assert_final_carbon=0.0,
    keep=0.0,
    keep_weight=0.5,
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
    car_soc=0,
    car_limit=100,
    set_charge_low_power=False,
    set_charge_window=True,
    battery_temperature=20,
    set_export_freeze_only=False,
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
    my_predbat.battery_temperature = battery_temperature
    my_predbat.battery_temperature_charge_curve = {
        20: 1.0,
        10: 0.5,
        9: 0.5,
        8: 0.5,
        7: 0.5,
        6: 0.3,
        5: 0.1,
        4: 0.08,
        3: 0.07,
        2: 0.05,
        1: 0.05,
        0: 0,
    }
    my_predbat.battery_temperature_discharge_curve = {
        20: 1.0,
        10: 0.5,
        9: 0.5,
        8: 0.5,
        7: 0.5,
        6: 0.3,
        5: 0.3,
        4: 0.3,
        3: 0.3,
        2: 0.3,
        1: 0.3,
        0: 0.3,
        -1: 0.2,
        -2: 0.2,
        -3: 0.2,
        -4: 0.2,
        -5: 0.2,
        -6: 0.2,
        -7: 0.1,
        -8: 0.1,
        -9: 0.05,
        -10: 0.01,
    }
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
    my_predbat.set_charge_low_power = set_charge_low_power
    my_predbat.set_charge_window = set_charge_window
    my_predbat.set_export_freeze_only = set_export_freeze_only

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
    my_predbat.iboost_smart_min_length = iboost_smart_min_length
    my_predbat.iboost_on_export = iboost_on_export
    my_predbat.iboost_prevent_discharge = iboost_prevent_discharge
    my_predbat.rate_gas = {n: rate_gas for n in range(my_predbat.forecast_minutes + my_predbat.minutes_now)}
    my_predbat.iboost_gas_scale = gas_scale
    my_predbat.iboost_charging = iboost_charging
    my_predbat.best_soc_keep = keep
    my_predbat.best_soc_keep_weight = keep_weight
    my_predbat.car_charging_soc[0] = car_soc
    my_predbat.car_charging_limit[0] = car_limit

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
    if abs(metric_keep - assert_keep) >= 0.5:
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
        self.battery_rate_max_discharge = 1.0
        self.battery_rate_max = 1.0 * 60 * 1000
        self.log = log
        self.id = inverter_id
        self.count_register_writes = 0
        self.reserve = 0

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
        self.inv_has_discharge_enable_time = True
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
        self.charge_window = []
        self.charge_limits = []
        self.export_window = []
        self.export_limits = []
        self.inv_support_discharge_freeze = True
        self.inv_support_charge_freeze = True
        self.inv_has_reserve_soc = True
        self.current_charge_limit = 0
        self.charge_rate_now = 1000
        self.discharge_rate_now = 1000
        self.battery_rate_min = 0
        self.inverter_limit = 1000
        self.export_limit = 1000
        self.pv_power = 0
        self.load_power = 0
        self.reserve_percent = 0
        self.reserve = 0
        self.reserve_last = -1
        self.reserve_current = 0
        self.reserve_percent = 0
        self.reserve_percent_current = 0
        self.battery_temperature = 20

    def update_status(self, minutes_now):
        pass

    def find_charge_curve(self, discharge=False):
        return None

    def get_current_charge_rate(self):
        return self.charge_rate

    def disable_charge_window(self):
        self.charge_time_enable = False

    def adjust_charge_window(self, charge_start_time, charge_end_time, minutes_now):
        self.charge_start_time_minutes = (charge_start_time - self.midnight_utc).total_seconds() / 60
        self.charge_end_time_minutes = (charge_end_time - self.midnight_utc).total_seconds() / 60
        self.charge_time_enable = True
        # print("Charge start_time {} charge_end_time {}".format(self.charge_start_time_minutes, self.charge_end_time_minutes))

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
        # print("Force export {} start_time {} end_time {}".format(self.force_export, self.discharge_start_time_minutes, self.discharge_end_time_minutes))

    def adjust_idle_time(self, charge_start=None, charge_end=None, discharge_start=None, discharge_end=None):
        self.idle_charge_start = charge_start
        self.idle_charge_end = charge_end
        self.idle_discharge_start = discharge_start
        self.idle_discharge_end = discharge_end

    def adjust_inverter_mode(self, force_export, changed_start_end=False):
        self.force_export = force_export
        self.changed_start_end = changed_start_end

    def adjust_reserve(self, reserve):
        self.reserve_last = reserve
        self.reserve_current = max(reserve, self.reserve)
        self.reserve_percent_current = calc_percent_limit(self.reserve_current, self.soc_max)

    def adjust_pause_mode(self, pause_charge=False, pause_discharge=False):
        self.pause_charge = pause_charge
        self.pause_discharge = pause_discharge

    def adjust_battery_target(self, soc, isCharging=False, isExporting=False):
        self.soc_target = soc
        self.current_charge_limit = soc
        self.isCharging = isCharging
        self.isExporting = isExporting

    def adjust_charge_rate(self, charge_rate):
        self.charge_rate = charge_rate
        self.charge_rate_now = charge_rate

    def adjust_discharge_rate(self, discharge_rate):
        self.discharge_rate = discharge_rate
        self.discharge_rate_now = discharge_rate


def run_execute_test(
    my_predbat,
    name,
    charge_window_best=[],
    charge_limit_best=[],
    export_window_best=[],
    export_limits_best=[],
    car_slot=[],
    soc_kw=0,
    soc_max=10,
    car_charging_from_battery=False,
    read_only=False,
    set_soc_enable=True,
    set_charge_window=False,
    set_export_window=False,
    set_charge_low_power=False,
    set_export_low_power=False,
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
    assert_soc_target_array=None,
    in_calibration=False,
    set_discharge_during_charge=True,
    assert_immediate_soc_target=None,
    assert_immediate_soc_target_array=None,
    set_reserve_enable=True,
    has_timed_pause=True,
    has_target_soc=True,
    has_charge_enable_time=True,
    inverter_hybrid=False,
    battery_max_rate=1000,
    minutes_now=12 * 60,
    update_plan=False,
    reserve=1,
    soc_kw_array=None,
    reserve_max=100,
    car_soc=0,
    battery_temperature=20,
):
    print("Run scenario {}".format(name))
    failed = False
    my_predbat.set_read_only = read_only
    my_predbat.car_charging_slots = [car_slot]
    my_predbat.num_cars = 1
    my_predbat.inverter_hybrid = inverter_hybrid
    my_predbat.set_charge_low_power = set_charge_low_power
    my_predbat.set_export_low_power = set_export_low_power
    my_predbat.charge_low_power_margin = charge_low_power_margin
    my_predbat.minutes_now = minutes_now
    my_predbat.battery_temperature_charge_curve = {20: 1.0, 10: 0.5, 9: 0.5, 8: 0.5, 7: 0.5, 6: 0.3, 5: 0.3, 4: 0.3, 3: 0.262, 2: 0.1, 1: 0.1, 0: 0}

    charge_window_best = charge_window_best.copy()
    charge_limit_best = charge_limit_best.copy()
    export_window_best = export_window_best.copy()
    export_limits_best = export_limits_best.copy()

    if assert_immediate_soc_target is None:
        assert_immediate_soc_target = assert_soc_target
    if assert_charge_rate is None:
        assert_charge_rate = battery_max_rate
    if assert_discharge_rate is None:
        assert_discharge_rate = battery_max_rate

    total_inverters = len(my_predbat.inverters)
    my_predbat.battery_rate_max_charge = battery_max_rate / 1000.0 * total_inverters / 60.0
    my_predbat.battery_rate_max_discharge = battery_max_rate / 1000.0 * total_inverters / 60.0
    my_predbat.set_reserve_enable = set_reserve_enable
    for inverter in my_predbat.inverters:
        inverter.charge_start_time_minutes = inverter_charge_time_minutes_start
        if soc_kw_array:
            inverter.soc_kw = soc_kw_array[inverter.id]
        else:
            inverter.soc_kw = soc_kw / total_inverters
        inverter.soc_max = soc_max / total_inverters
        inverter.soc_percent = calc_percent_limit(inverter.soc_kw, inverter.soc_max)
        inverter.in_calibration = in_calibration
        inverter.battery_rate_max_charge = my_predbat.battery_rate_max_charge / total_inverters
        inverter.battery_rate_max_discharge = my_predbat.battery_rate_max_discharge / total_inverters
        inverter.inv_has_timed_pause = has_timed_pause
        inverter.inv_has_target_soc = has_target_soc
        inverter.inv_has_charge_enable_time = has_charge_enable_time
        reserve_kwh = reserve / total_inverters
        reserve_percent = calc_percent_limit(reserve_kwh, inverter.soc_max)
        inverter.reserve_percent = reserve_percent
        inverter.reserve_current = reserve_percent
        inverter.reserve_percent_current = reserve_percent
        inverter.reserve = reserve_kwh
        inverter.reserve_max = reserve_max
        inverter.battery_temperature = battery_temperature

    my_predbat.fetch_inverter_data(create=False)

    if my_predbat.soc_kw != soc_kw:
        print("ERROR: Predbat level SOC should be {} got {}".format(soc_kw, my_predbat.soc_kw))
        failed = True
    if my_predbat.soc_percent != calc_percent_limit(my_predbat.soc_kw, my_predbat.soc_max):
        print("ERROR: Predbat level SOC percent should be {} got {}".format(calc_percent_limit(my_predbat.soc_kw, my_predbat.soc_max), my_predbat.soc_percent))
        failed = True
    if my_predbat.soc_max != soc_max:
        print("ERROR: Predbat level SOC max should be {} got {}".format(soc_max, my_predbat.soc_max))
        failed = True

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
    my_predbat.car_charging_from_battery = car_charging_from_battery
    my_predbat.car_charging_soc[0] = car_soc

    # Shift on plan?
    if update_plan:
        my_predbat.plan_last_updated = my_predbat.now_utc
        my_predbat.args["threads"] = 0
        my_predbat.calculate_plan(recompute=False)

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
        if assert_reserve != inverter.reserve_last:
            print("ERROR: Inverter {} Reserve should be {} got {}".format(inverter.id, assert_reserve, inverter.reserve_last))
            failed = True
        if assert_soc_target_array:
            assert_soc_target = assert_soc_target_array[inverter.id]
        if assert_soc_target != inverter.soc_target:
            print("ERROR: Inverter {} SOC target should be {} got {}".format(inverter.id, assert_soc_target, inverter.soc_target))
            failed = True

        if assert_immediate_soc_target_array:
            assert_immediate_soc_target = assert_immediate_soc_target_array[inverter.id]

        assert_soc_target_force = (
            assert_immediate_soc_target if assert_status in ["Charging", "Hold charging", "Freeze charging", "Hold charging, Hold for iBoost", "Hold charging, Hold for car", "Freeze charging, Hold for iBoost", "Hold for car", "Hold for iBoost"] else 0
        )
        if not set_charge_window:
            assert_soc_target_force = -1
        if inverter.immediate_charge_soc_target != assert_soc_target_force:
            print("ERROR: Inverter {} Immediate charge SOC target should be {} got {}".format(inverter.id, assert_soc_target_force, inverter.immediate_charge_soc_target))
            failed = True
        if assert_status in ["Freeze charging"] and inverter.immediate_charge_soc_freeze != True:
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

    my_predbat.minutes_now = 12 * 60
    return failed


def run_single_debug(test_name, my_predbat, debug_file, expected_file=None, compare=False):
    print("**** Running debug test {} ****\n".format(debug_file))
    if not expected_file:
        re_do_rates = True
        reset_load_model = True
    else:
        reset_load_model = False
        re_do_rates = False
    load_override = 1.0
    my_predbat.load_user_config()
    failed = False

    print("**** Test {} ****".format(test_name))
    reset_inverter(my_predbat)
    my_predbat.read_debug_yaml(debug_file)
    my_predbat.config_root = "./"
    my_predbat.save_restore_dir = "./"
    my_predbat.load_user_config()
    my_predbat.args["threads"] = 0
    # my_predbat.fetch_config_options()

    # Force off combine export XXX:
    print("Combined export slots {} min_improvement_export {} set_export_freeze_only {}".format(my_predbat.combine_export_slots, my_predbat.metric_min_improvement_export, my_predbat.set_export_freeze_only))
    if not expected_file:
        my_predbat.plan_debug = True
        # my_predbat.set_discharge_during_charge = True
        # my_predbat.calculate_export_oncharge = True
        # my_predbat.combine_charge_slots = False
        my_predbat.metric_min_improvement_export = 0.1
        # my_predbat.set_reserve_min = 0

        # my_predbat.metric_self_sufficiency = 5
        # my_predbat.calculate_second_pass = False
        # my_predbat.best_soc_keep = 0
        # my_predbat.set_charge_freeze = True
        # my_predbat.combine_export_slots = False
        # my_predbat.inverter_loss = 0.97
        # my_predbat.calculate_tweak_plan = False

        # my_predbat.inverter_loss = 0.97
        # my_predbat.calculate_second_pass = False
        # my_predbat.metric_battery_cycle = 0
        # my_predbat.carbon_enable = False
        # my_predbat.metric_battery_value_scaling = 0.90
        my_predbat.manual_export_times = []
        my_predbat.manual_all_times = []
        my_predbat.manual_charge_times = []
        # my_predbat.set_export_low_power = True
        pass

    if re_do_rates:
        # Set rate thresholds
        if my_predbat.rate_import or my_predbat.rate_export:
            print("Set rate thresholds")
            my_predbat.set_rate_thresholds()
            print("Result export {} import {}".format(my_predbat.rate_export_cost_threshold, my_predbat.rate_import_cost_threshold))

        # Find discharging windows
        if my_predbat.rate_export:
            my_predbat.high_export_rates, export_lowest, export_highest = my_predbat.rate_scan_window(my_predbat.rate_export, 5, my_predbat.rate_export_cost_threshold, True)
            print("High export rate found rates in range {} to {} based on threshold {}".format(export_lowest, export_highest, my_predbat.rate_export_cost_threshold))
            print("Export windows {}".format(my_predbat.high_export_rates))
            # Update threshold automatically
            if my_predbat.rate_high_threshold == 0 and export_lowest <= my_predbat.rate_export_max:
                my_predbat.rate_export_cost_threshold = export_lowest

        # Find charging windows
        if my_predbat.rate_import:
            # Find charging window
            print("rate scan window import threshold rate {}".format(my_predbat.rate_import_cost_threshold))
            my_predbat.low_rates, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False)
            # Update threshold automatically
            if my_predbat.rate_low_threshold == 0 and highest >= my_predbat.rate_min:
                my_predbat.rate_import_cost_threshold = highest

            print("Lowest rate {} highest rate {} rates {}".format(lowest, highest, my_predbat.low_rates))

    print("minutes_now {} end_record {}".format(my_predbat.minutes_now, my_predbat.end_record))

    if compare:
        print("Run compare")
        compare_tariffs = [
            {"name": "Fixed exports", "rates_export": [{"rate": 15.0}], "config": {"load_scaling": 2.0}},
            {"name": "Agile export", "rates_export_octopus_url": "https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/"},
        ]
        my_predbat.args["compare"] = compare_tariffs
        compare = Compare(my_predbat)
        compare.run_all(debug=True)
        return

    # Reset load model
    if reset_load_model:
        print("Reset load model")
        my_predbat.load_minutes_step = my_predbat.step_data_history(
            my_predbat.load_minutes,
            my_predbat.minutes_now,
            forward=False,
            scale_today=my_predbat.load_inday_adjustment,
            scale_fixed=1.0 * load_override,
            type_load=True,
            load_forecast=my_predbat.load_forecast,
            load_scaling_dynamic=my_predbat.load_scaling_dynamic,
            cloud_factor=my_predbat.metric_load_divergence,
        )
        my_predbat.load_minutes_step10 = my_predbat.step_data_history(
            my_predbat.load_minutes,
            my_predbat.minutes_now,
            forward=False,
            scale_today=my_predbat.load_inday_adjustment,
            scale_fixed=my_predbat.load_scaling10 * load_override,
            type_load=True,
            load_forecast=my_predbat.load_forecast,
            load_scaling_dynamic=my_predbat.load_scaling_dynamic,
            cloud_factor=min(my_predbat.metric_load_divergence + 0.5, 1.0) if my_predbat.metric_load_divergence else None,
        )
        my_predbat.pv_forecast_minute_step = my_predbat.step_data_history(my_predbat.pv_forecast_minute, my_predbat.minutes_now, forward=True, cloud_factor=my_predbat.metric_cloud_coverage)
        my_predbat.pv_forecast_minute10_step = my_predbat.step_data_history(my_predbat.pv_forecast_minute10, my_predbat.minutes_now, forward=True, cloud_factor=min(my_predbat.metric_cloud_coverage + 0.2, 1.0) if my_predbat.metric_cloud_coverage else None)

    pv_step = my_predbat.pv_forecast_minute_step
    pv10_step = my_predbat.pv_forecast_minute10_step
    load_step = my_predbat.load_minutes_step
    load10_step = my_predbat.load_minutes_step10

    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.debug_enable = True

    failed = False
    my_predbat.log("> ORIGINAL PLAN")
    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, False, end_record=my_predbat.end_record, save="best"
    )

    # Show setting changes
    if not expected_file:
        for item in my_predbat.CONFIG_ITEMS:
            name = item["name"]
            value = item.get("value", None)
            default = item.get("default", None)
            enable = item.get("enable", None)
            enabled = my_predbat.user_config_item_enabled(item)
            if enabled and value != default:
                print("- {} = {} (default {}) - enable {}".format(name, value, default, enable))

    # Save plan
    # Pre-optimise all plan
    my_predbat.charge_limit_percent_best = calc_percent_limit(my_predbat.charge_limit_best, my_predbat.soc_max)
    my_predbat.update_target_values()
    my_predbat.publish_html_plan(pv_step, pv10_step, load_step, load10_step, my_predbat.end_record)
    filename = "plan_orig.html"
    open(filename, "w").write(my_predbat.html_plan)
    print("Wrote plan to {}".format(filename))

    print("Export windows {}".format(my_predbat.export_window_best))
    my_predbat.calculate_plan(recompute=True, debug_mode=True)

    # Predict
    my_predbat.log("> FINAL PLAN")
    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, False, end_record=my_predbat.end_record, save="best"
    )
    my_predbat.log("Final plan soc_min {} final_soc {}".format(soc_min, soc))

    my_predbat.publish_html_plan(pv_step, pv10_step, load_step, load10_step, my_predbat.end_record)
    filename = "plan_final.html"
    open(filename, "w").write(my_predbat.html_plan)
    print("Wrote plan to {}".format(filename))

    # Expected
    actual_data = {"charge_limit_best": my_predbat.charge_limit_best, "charge_window_best": my_predbat.charge_window_best, "export_window_best": my_predbat.export_window_best, "export_limits_best": my_predbat.export_limits_best}
    actual_json = json.dumps(actual_data)
    if expected_file:
        print("Compare with {}".format(expected_file))
        if not os.path.exists(expected_file):
            failed = True
            print("ERROR: Expected file {} does not exist".format(expected_file))
        else:
            expected_data = json.loads(open(expected_file).read())
            expected_json = json.dumps(expected_data)
            if actual_json != expected_json:
                print("ERROR: Actual plan does not match expected plan")
                failed = True
    # Write actual plan
    filename = test_name + ".actual.json"
    open(filename, "w").write(actual_json)
    print("Wrote plan json to {}".format(filename))

    my_predbat.create_debug_yaml(write_file=True)

    return failed


def run_test_ge_cloud(my_predbat):
    """
    GE Cloud test
    """
    failed = False

    ge_cloud_direct = GECloudDirect(my_predbat)
    ge_cloud_direct_task = my_predbat.create_task(ge_cloud_direct.start())
    while not "devices" in ge_cloud_direct.__dict__:
        time.sleep(1)
    devices = ge_cloud_direct.devices
    if not devices:
        print("ERROR: No devices found")
        failed = True
    else:
        for device in devices:
            print("Device {} found:".format(device))
            while not ge_cloud_direct.settings.get(device):
                time.sleep(1)
            print("Device {} synced".format(device))

        my_predbat.create_task(ge_cloud_direct.switch_event("switch.predbat_gecloud_sa2243g277_ac_charge_enable", "turn_on"))
        time.sleep(1)
    print("Stopping cloud")
    ge_cloud_direct.stop_cloud = True
    time.sleep(1)

    return failed


def run_test_web_if(my_predbat):
    """
    Test the web interface
    """
    failed = 0
    print("**** Running web interface test ****\n")
    ha = my_predbat.ha_interface
    my_predbat.web_interface = WebInterface(my_predbat)
    my_predbat.web_interface_task = my_predbat.create_task(my_predbat.web_interface.start())

    # Fetch page from 127.0.0.1:5052
    for page in ["/", "/dash", "/plan", "/config", "/apps", "/charts", "/compare", "/log"]:
        print("Fetch page {}".format(page))
        address = "http://127.0.0.1:5052" + page
        res = requests.get(address)
        if res.status_code != 200:
            print("ERROR: Failed to fetch from page {} got status {} value {}".format(address, res.status_code, res.text))
            failed = 1

    # Perform a post to /compare page with data for form 'compareform' value 'run'
    print("**** Running test: Fetch page /compare with post")

    address = "http://127.0.0.1:5052/compare"
    data = {"run": "run"}
    res = requests.post(address, data=data)
    if res.status_code != 200:
        print("ERROR: Failed to post to pagepage {} got status {} value {}".format(address, res.status_code, res.text))
        failed = 1
    time.sleep(0.1)
    # Get service data
    entity_id = "switch.predbat_compare_active"
    result = ha.get_state(entity_id)

    if result != "on":
        print("ERROR: Compare tariffs not triggered - expected {} got {}".format("on", result))
        failed = 1

    my_predbat.web_interface.abort = True
    return failed


def run_execute_tests(my_predbat):
    print("**** Running execute tests ****\n")
    reset_inverter(my_predbat)

    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best_soon = [{"start": my_predbat.minutes_now + 5, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best2 = [{"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best3 = [{"start": my_predbat.minutes_now - 30, "end": my_predbat.minutes_now, "average": 1}, {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best4 = [{"start": my_predbat.minutes_now + 24 * 60, "end": my_predbat.minutes_now + 60 + 24 * 60, "average": 1}]
    charge_window_best5 = [{"start": my_predbat.minutes_now - 24 * 60, "end": my_predbat.minutes_now + 60, "average": 1}]
    charge_window_best6 = [{"start": my_predbat.minutes_now + 8 * 60, "end": my_predbat.minutes_now + 60 + 8 * 60, "average": 1}]
    charge_window_best7 = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 23 * 60, "average": 1}]
    charge_window_best8 = [{"start": 0, "end": my_predbat.minutes_now + 12 * 60, "average": 1}]
    charge_window_best9 = [{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 90, "average": 1}]
    charge_window_best_short = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 15, "average": 1}]
    charge_limit_best0 = [10]
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
    export_limits_best3 = [50.5]
    export_limits_best_frz = [99]

    inverters = [ActiveTestInverter(0, 0, 10.0, my_predbat.now_utc), ActiveTestInverter(1, 0, 10.0, my_predbat.now_utc)]
    my_predbat.inverters = inverters
    my_predbat.args["num_inverters"] = 2

    failed = False
    failed |= run_execute_test(my_predbat, "off", assert_reserve=-1)
    my_predbat.holiday_days_left = 2
    failed |= run_execute_test(my_predbat, "off_holiday", assert_status="Demand (Holiday)", assert_reserve=-1)
    my_predbat.holiday_days_left = 0

    failed |= run_execute_test(my_predbat, "no_charge", charge_window_best=charge_window_best, charge_limit_best=charge_limit_best, assert_reserve=-1)
    failed |= run_execute_test(my_predbat, "no_charge2", set_charge_window=True, set_export_window=True, set_discharge_during_charge=False)
    failed |= run_execute_test(my_predbat, "no_charge3", set_charge_window=True, set_export_window=True, set_discharge_during_charge=False, has_timed_pause=False)
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
    failed |= run_execute_test(my_predbat, "no_charge_iboost", set_charge_window=True, set_export_window=True, assert_pause_discharge=True, assert_status="Hold for iBoost", soc_kw=1, assert_immediate_soc_target=10)

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
        "charge_imbalance",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw=9.5,
        soc_kw_array=[5, 4.5],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw=9.5,
        soc_kw_array=[4.5, 5],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance3",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw=5,
        soc_kw_array=[2.0, 3.0],
        assert_soc_target_array=[40, 60],
        assert_immediate_soc_target=50,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance4",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw=6,
        soc_kw_array=[3.0, 3.0],
        assert_soc_target_array=[50, 50],
        assert_immediate_soc_target=50,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_imbalance5",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw=7,
        soc_kw_array=[3.0, 4.0],
        assert_soc_target_array=[40, 60],
        assert_immediate_soc_target=50,
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
        assert_charge_rate=1300,  # Keep current rate as it is over the max rate we will achieve anyhow
        battery_max_rate=2000,
    )
    if failed:
        return failed

    # No impact at 10 degrees
    failed |= run_execute_test(
        my_predbat,
        "charge_low_power_temp1",
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
        battery_temperature=10,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_low_power_temp2",
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
        battery_temperature=3,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_low_power_temp3",
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
        assert_charge_rate=2000,
        battery_max_rate=2000,
        battery_temperature=1,
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
        "charge2d",
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
        "charge2e",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Charging",
        soc_kw=0,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=0,
        assert_pause_discharge=False,
        assert_reserve=0,
        assert_immediate_soc_target=50,
        assert_charge_time_enable=True,
        assert_soc_target=50,
        has_timed_pause=False,
        set_discharge_during_charge=False,
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

    failed |= run_execute_test(
        my_predbat,
        "charge_hold_reserve_max1",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=1000,
        assert_pause_discharge=False,
        assert_soc_target=50,
        reserve_max=50,
        has_timed_pause=False,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge_hold_reserve_max2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best2,
        assert_charge_time_enable=True,
        set_charge_window=True,
        set_export_window=True,
        assert_status="Hold charging",
        soc_kw=9,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        assert_discharge_rate=1000,
        assert_pause_discharge=False,
        assert_soc_target_array=[60, 40],
        assert_immediate_soc_target=50,
        reserve_max=90,
        has_timed_pause=False,
        soc_kw_array=[5, 4],
    )

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
    failed |= run_execute_test(my_predbat, "charge_read_only", charge_window_best=charge_window_best, charge_limit_best=charge_limit_best, set_charge_window=True, set_export_window=True, read_only=True, assert_status="Read-Only", reserve=0)
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
        reserve=0,
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
        "charge_freeze1c",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=1,
        assert_pause_discharge=True,
        assert_status="Freeze charging",
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=10,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze1d_too_low",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable=True,
        soc_kw=0,
        assert_pause_discharge=False,
        assert_status="Charging",
        assert_reserve=0,
        assert_soc_target=10,
        assert_immediate_soc_target=10,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb1",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable=True,
        soc_kw=2,
        assert_pause_discharge=False,
        assert_status="Hold charging",
        assert_reserve=0,
        assert_soc_target_array=[10, 40],
        assert_immediate_soc_target_array=[10, 40],
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw_array=[0, 2],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb2",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable=True,
        soc_kw=2,
        assert_pause_discharge=False,
        assert_status="Hold charging",
        assert_reserve=0,
        assert_soc_target_array=[40, 10],
        assert_immediate_soc_target_array=[40, 10],
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw_array=[2, 0],
    )
    if failed:
        return failed

    # Target SOC can not be lower than reserve (which is 1) so it will charge to 1 not freeze
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb3",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable=True,
        soc_kw=0.75,
        assert_pause_discharge=False,
        assert_status="Charging",
        assert_reserve=0,
        assert_soc_target_array=[10, 10],
        assert_immediate_soc_target=10,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw_array=[0.5, 0.25],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb4",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable=True,
        soc_kw=1,
        assert_pause_discharge=False,
        assert_status="Hold charging",
        assert_reserve=0,
        assert_soc_target_array=[10, 15],
        assert_immediate_soc_target_array=[10, 15],
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 60,
        soc_kw_array=[0.25, 0.75],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_imb5",
        charge_window_best=charge_window_best,
        charge_limit_best=charge_limit_best_frz,
        set_charge_window=True,
        set_export_window=True,
        assert_charge_time_enable=False,
        soc_kw=1,
        assert_pause_discharge=True,
        assert_status="Freeze charging",
        assert_reserve=0,
        assert_soc_target_array=[100, 100],
        assert_immediate_soc_target=10,
        soc_kw_array=[0.5, 0.5],
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_soon",
        charge_window_best=charge_window_best_soon,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_pause_discharge=False,
        assert_status="Demand",
        assert_discharge_rate=1000,
        assert_reserve=0,
        assert_soc_target=100,
        assert_immediate_soc_target=100,
    )
    failed |= run_execute_test(
        my_predbat,
        "charge_freeze_soon2",
        charge_window_best=charge_window_best_soon,
        charge_limit_best=charge_limit_best_frz,
        assert_charge_time_enable=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        has_target_soc=False,
        assert_pause_discharge=False,
        assert_status="Demand",
        assert_discharge_rate=1000,
        assert_reserve=0,
        assert_soc_target=0,
        assert_immediate_soc_target=100,
    )

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
        assert_reserve=0,
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
        reserve=1,
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

    failed |= run_execute_test(my_predbat, "no_discharge", export_window_best=export_window_best, export_limits_best=export_limits_best, assert_reserve=-1)
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
        "discharge_car_full_bat",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=100,
        assert_status="Exporting",
        car_slot=charge_window_best,
        car_charging_from_battery=True,
        assert_force_export=True,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        assert_immediate_soc_target=0,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_discharge_car_demand1",
        set_charge_window=True,
        set_export_window=True,
        soc_kw=100,
        assert_status="Hold for car",
        assert_pause_discharge=True,
        car_slot=charge_window_best,
        assert_immediate_soc_target=100,
        car_charging_from_battery=False,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "no_discharge_car_demand2",
        set_charge_window=True,
        set_export_window=True,
        soc_kw=100,
        assert_status="Demand",
        assert_pause_discharge=False,
        car_slot=charge_window_best,
        car_charging_from_battery=False,
        car_soc=100,
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
        "discharge_charge1",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        charge_limit_best=charge_limit_best0,
        charge_window_best=charge_window_best9,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=9,
        assert_status="Exporting",
        assert_immediate_soc_target=0,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        minutes_now=775,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge_charge2",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best,
        charge_limit_best=charge_limit_best0,
        charge_window_best=charge_window_best9,
        assert_force_export=False,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=9,
        assert_status="Charging",
        assert_immediate_soc_target=100,
        assert_charge_start_time_minutes=-1,
        assert_charge_end_time_minutes=my_predbat.minutes_now + 90,
        assert_charge_time_enable=True,
        minutes_now=780,
        update_plan=True,
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
        assert_discharge_rate=1000,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge4",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best3,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=50,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        assert_discharge_rate=1000,
    )
    if failed:
        return failed

    failed |= run_execute_test(
        my_predbat,
        "discharge5",
        export_window_best=export_window_best,
        export_limits_best=export_limits_best3,
        assert_force_export=True,
        set_charge_window=True,
        set_export_window=True,
        soc_kw=10,
        assert_status="Exporting",
        assert_immediate_soc_target=50,
        assert_discharge_start_time_minutes=my_predbat.minutes_now,
        assert_discharge_end_time_minutes=my_predbat.minutes_now + 60 + 1,
        assert_discharge_rate=500,
        set_export_low_power=True,
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
    failed |= run_execute_test(my_predbat, "car", car_slot=charge_window_best, set_charge_window=True, set_export_window=True, assert_status="Hold for car", assert_pause_discharge=True, assert_discharge_rate=1000, soc_kw=1, assert_immediate_soc_target=10)
    failed |= run_execute_test(
        my_predbat, "car2", car_slot=charge_window_best, set_charge_window=True, set_export_window=True, assert_status="Hold for car", assert_pause_discharge=False, assert_discharge_rate=0, has_timed_pause=False, soc_kw=1, assert_immediate_soc_target=10
    )
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
    rate_export=5.5,
    battery_size=100.0,
    battery_soc=0.0,
    hybrid=False,
    inverter_loss=1.0,
    best_soc_keep=0.0,
    best_soc_keep_weight=0.5,
    second_pass=False,
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
    my_predbat.best_soc_keep_weight = best_soc_keep_weight
    my_predbat.reserve = 0.5
    my_predbat.set_charge_freeze = True
    my_predbat.calculate_second_pass = second_pass

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

    if abs(metric - expect_best_price) >= 0.2:
        print("ERROR: Expected best price {} but got {}".format(expect_best_price, metric))
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
        "single_windows",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=[0],
        load_amount=1.0,
        pv_amount=0,
        expect_best_price=1 * 10 * 24,
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
        expect_charge_limit.append(10 if price < 5.0 else 0)
    expect_charge_limit[27] = 0.5  # freeze
    expect_charge_limit[43] = 0.5  # freeze
    failed |= run_optimise_all_windows(
        "created2_windows",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=31.6,
        inverter_loss=0.9,
        best_soc_keep=0.0,
        battery_size=10,
    )

    if failed:
        return failed

    # One extra charge as we will fall below keep otherwise
    expect_charge_limit[9] = 0.5
    expect_charge_limit[10] = 0.5
    expect_charge_limit[11] = 0.5
    failed |= run_optimise_all_windows(
        "created3_windows",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=20.8,
        inverter_loss=0.9,
        best_soc_keep=1,
        battery_soc=2,
        battery_size=10,
        second_pass=True,
    )
    if failed:
        return failed

    # Created4
    charge_window_best = []
    export_window_best = []
    expect_charge_limit = []
    expect_export_limit = []
    for n in range(0, 48):
        price = n % 16
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})
        expect_charge_limit.append(10 if price <= 5.0 * 0.9 else 0)

    expect_charge_limit[5] = 0.5
    expect_charge_limit[21] = 0.5
    expect_charge_limit[37] = 0.5
    failed |= run_optimise_all_windows(
        "created4_windows",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=19.5,
        inverter_loss=0.9,
        best_soc_keep=1,
        battery_soc=2,
        battery_size=10,
    )
    if failed:
        return failed

    # Optimise charge limit
    best_soc, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep, best_cycle, best_carbon, best_import = my_predbat.optimise_charge_limit(
        0, len(expect_charge_limit), expect_charge_limit, charge_window_best, export_window_best, expect_export_limit, all_n=None, end_record=my_predbat.end_record
    )
    before_best_metric = best_metric
    my_predbat.isCharging = True
    my_predbat.isCharging_Target = 100
    best_soc, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep, best_cycle, best_carbon, best_import = my_predbat.optimise_charge_limit(
        0, len(expect_charge_limit), expect_charge_limit, charge_window_best, export_window_best, expect_export_limit, all_n=None, end_record=my_predbat.end_record
    )

    if (before_best_metric - best_metric) < 0.099:
        print("ERROR: Expected best metric to have 0.1 skew for charging but got {} vs {} skew was {}".format(best_metric, before_best_metric, before_best_metric - best_metric))
        failed = True
    if best_cost != 19.5:
        print("ERROR: Expected best cost to be 19.5 but got {}".format(best_cost))
        failed = True
    my_predbat.isCharging = False

    # Low rate tes
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
        "off_peak_windows",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.5,
        pv_amount=0,
        expect_best_price=115.5,
        inverter_loss=0.9,
        best_soc_keep=0.5,
        battery_size=10,
        battery_soc=5,
        rate_export=15,
    )

    # Compare test
    print("**** Compare test ****")
    compare_tariffs = [
        {"id": "base", "name": "Base", "config": {"load_scaling": 1.0}},
        {"id": "double", "name": "Double Load", "config": {"load_scaling": 2.0}},
    ]
    my_predbat.args["compare_list"] = compare_tariffs
    compare = Compare(my_predbat)
    compare.run_all(debug=True, fetch_sensor=False)

    results = compare.comparisons
    if len(results) != 2:
        print("ERROR: Compare expected 2 results but got {}".format(len(results)))
        failed = True
    else:
        result0 = results.get("base", None)
        result1 = results.get("double", None)
        if not result0:
            print("ERROR: Compare expected result 0 to be valid")
            failed = True
        if not result1:
            print("ERROR: Compare eExpected result 1 to be valid")
            failed = True
    #    if result0['cost'] != 115.5:
    #        print("ERROR: Expected result 0 cost to be 115.5 but got {}".format(result0['cost']))
    #    if result1['cost'] != 231.0:
    #        failed = True
    #        print("ERROR: Expected result 1 cost to be 231.0 but got {}".format(result1['cost']))
    #        failed = True

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
        expect_charge_limit.append(100 if price <= 5.0 * 0.9 else 0)
    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "created1",
        my_predbat,
        charge_window_best=charge_window_best,
        export_window_best=export_window_best,
        expect_charge_limit=expect_charge_limit,
        expect_export_limit=expect_export_limit,
        load_amount=0.5,
        pv_amount=0,
        expect_best_price=4.0 / 0.9,
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
        expect_charge_limit.append(100 if price <= 5.0 * 0.9 else 0)
    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "created2",
        my_predbat,
        charge_window_best=charge_window_best,
        export_window_best=export_window_best,
        expect_charge_limit=expect_charge_limit,
        expect_export_limit=expect_export_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=4.0 / 0.9,
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
        best_battery_value,
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
    failed |= simple_scenario("load_bat_ac2", my_predbat, 0.5, 0, assert_final_metric=0, assert_final_soc=100 - 12 / 0.8, with_battery=True, battery_soc=100.0, inverter_loss=0.8)
    failed |= simple_scenario("load_bat_dc2", my_predbat, 0.5, 0, assert_final_metric=0, assert_final_soc=100 - 12 / 0.8, with_battery=True, battery_soc=100.0, inverter_loss=0.8, hybrid=True)
    failed |= simple_scenario("load_bat_ac3", my_predbat, 1.0, 0, assert_final_metric=import_rate * 0.2 * 24, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8)
    failed |= simple_scenario("load_bat_dc3", my_predbat, 1.0, 0, assert_final_metric=import_rate * 0.2 * 24, assert_final_soc=100 - 24, with_battery=True, battery_soc=100.0, inverter_loss=0.8, hybrid=True)

    failed |= simple_scenario("load_empty_bat1", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5, assert_final_soc=4, with_battery=True, battery_soc=4, reserve=4)
    failed |= simple_scenario("load_empty_bat2", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5, assert_final_soc=3, with_battery=True, battery_soc=3, reserve=4)
    failed |= simple_scenario("load_empty_bat_chrg1", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5 + import_rate * 1, assert_final_soc=4, with_battery=True, battery_soc=3, reserve=4, charge=4)
    failed |= simple_scenario("load_empty_bat_chrg2", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5 + import_rate * 2, assert_final_soc=5, with_battery=True, battery_soc=3, reserve=4, charge=5)
    failed |= simple_scenario("load_empty_bat_chrg3", my_predbat, 0.5, 0, assert_final_metric=import_rate * 24 * 0.5, assert_final_soc=5, with_battery=True, battery_soc=5, reserve=4, charge=5)

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
    failed |= simple_scenario(
        "load_car_bat_no3",
        my_predbat,
        1,
        0,
        assert_final_metric=import_rate * 3,
        assert_final_soc=100.0 - 24,
        with_battery=True,
        charge_car=60,
        car_soc=97.0,
        battery_soc=100.0,
        car_charging_from_battery=False,
    )

    failed |= simple_scenario("load_discharge", my_predbat, 1, 0, assert_final_metric=import_rate * 14, assert_final_soc=0, battery_soc=10.0, with_battery=True)
    failed |= simple_scenario("load_discharge2", my_predbat, 1, 0, assert_final_metric=0, assert_final_soc=100 - 24, battery_soc=100.0, with_battery=True)
    failed |= simple_scenario("load_discharge3", my_predbat, 1, 0, assert_final_metric=0, assert_final_soc=100 - 48, battery_soc=100.0, with_battery=True, battery_loss=0.5)
    failed |= simple_scenario("load_discharge4", my_predbat, 1, 0, assert_final_metric=import_rate * 14, assert_final_soc=0, battery_soc=100.0, with_battery=True, battery_loss=0.1)

    # Discharge curve has 0.05 for -9 which is 0.5 max rate
    failed |= simple_scenario("discharge_curve1", my_predbat, 1, 0, assert_final_metric=import_rate * 20 * 0.5 + 4 * import_rate, assert_final_soc=0, battery_soc=10.0, with_battery=True, battery_size=10, battery_temperature=-9)
    # Discharge curve has 0.01 for -10 which is 0.1 max rate
    failed |= simple_scenario("discharge_curve2", my_predbat, 1, 0, assert_final_metric=import_rate * 24 * 0.90, assert_final_soc=7.6, battery_soc=10.0, with_battery=True, battery_temperature=-10, battery_size=10)

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

    failed |= simple_scenario("battery_charge_low_off", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=False, keep=5, assert_keep=24.59)
    failed |= simple_scenario("battery_charge_low_on", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=True, keep=5, assert_keep=88.89)
    failed |= simple_scenario(
        "battery_charge_low_on_monitor", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=True, keep=5, assert_keep=24.59, set_charge_window=False
    )

    failed |= simple_scenario(
        "battery_charge_low_temp1", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=False, keep=5, assert_keep=24.59, battery_temperature=20
    )
    failed |= simple_scenario(
        "battery_charge_low_temp2", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=False, keep=5, assert_keep=80.00, battery_temperature=1
    )
    failed |= simple_scenario(
        "battery_charge_low_temp3", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10, set_charge_low_power=True, keep=5, assert_keep=88.89, battery_temperature=1
    )

    if failed:
        return failed
    failed |= simple_scenario("battery_charge_prev_charge", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)

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
        assert_keep=14 * import_rate * 0.5 + ((1 + (1 / 12)) * import_rate * 0.5 * 0.5),
        keep=1,
        keep_weight=0.5,
    )
    failed |= simple_scenario(
        "battery_discharge_keep2",
        my_predbat,
        0,
        0,
        assert_final_metric=-export_rate * 1,
        assert_final_soc=0,
        with_battery=True,
        discharge=0,
        battery_soc=1,
        assert_keep=23 * import_rate * 0.5 + ((1 + (1 / 12)) * import_rate * 0.5 * 0.5),
        keep=1,
        keep_weight=0.5,
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
        assert_keep=14 * import_rate + 1 * import_rate * 0.5,
        keep=1.0,
        keep_weight=1.0,
    )
    failed |= simple_scenario(
        "battery_load_keep_four_hour",
        my_predbat,
        1.0,
        0,
        assert_final_metric=import_rate * 20,
        assert_final_soc=0,
        with_battery=True,
        battery_soc=4,
        assert_keep=20 * import_rate * 4 + 53,
        keep=4.0,
        keep_weight=1.0,
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
        assert_keep=14 * import_rate * 0.8 + 1 * import_rate * 0.8 * 0.5,
        keep=1.0,
        keep_weight=0.8,
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
        assert_keep=14 * import_rate * 0.8 + 1 * import_rate * 0.8 * 0.5,
        keep=1.0,
        keep_weight=0.8,
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
    failed |= simple_scenario("battery_discharge_freeze2", my_predbat, 0, 0.5, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=10, with_battery=True, discharge=99, battery_soc=10, set_export_freeze_only=True)
    failed |= simple_scenario("battery_discharge_freeze_only", my_predbat, 0, 0.5, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=10, with_battery=True, discharge=0, battery_soc=10, set_export_freeze_only=True)

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
        keep_weight=1.0,
        assert_final_iboost=0,
        assert_keep=import_rate * 14 + import_rate * 1 * 0.5,
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
        "iboost_smart1",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 120,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_charging=False,
        iboost_smart=True,
        assert_final_iboost=120,
        iboost_max_energy=60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_smart2",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 120 * 1.5,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_charging=False,
        iboost_smart=True,
        assert_final_iboost=120,
        iboost_max_energy=60,
        iboost_smart_min_length=60,
        assert_iboost_running=True,
        assert_iboost_running_full=True,
    )
    failed |= simple_scenario(
        "iboost_smart3",
        my_predbat,
        0,
        0,
        assert_final_metric=import_rate * 120 * 1.5 - 2 * import_rate * 5 * 2,
        assert_final_soc=0,
        with_battery=False,
        iboost_enable=True,
        iboost_charging=False,
        iboost_smart=True,
        assert_final_iboost=110,
        iboost_max_energy=55,
        iboost_smart_min_length=60,
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


def test_find_charge_rate(my_predbat):
    failed = 0

    # 2025-01-20 04:41:34.362134: Inverter 0 Charge window will be: 2025-01-19 23:30:00+00:00 - 2025-01-20 05:30:00+00:00 - current soc 95 target 100
    # 2025-01-20 04:41:34.364437: Low Power mode: minutes left: 40 absolute: 50 SOC: 9.04 Target SOC: 9.52 Charge left: 0.48 Max rate: 2600.0 Min rate: 576.0 Best rate: 2600.0 Best rate real: 1274.0 Battery temp 17.0
    # 2025-01-20 04:41:34.364497: Inverter 0 Target SOC 100 (this inverter 100.0) Battery temperature 17.0 Select charge rate 2600w (real 1274.0w) current charge rate 952
    # 2025-01-20 04:41:34.364549: Inverter 0 current charge rate is 952W and new target is 2600W
    # 2025-01-20 04:41:34.695878: Inverter 0 set charge rate 2600 via REST successful on retry 0

    current_charge_rate = 952
    soc = 9.04
    soc_max = 9.52
    log_to = print  # my_predbat.log
    minutes_now = my_predbat.minutes_now
    window = {"start": minutes_now - 60, "end": minutes_now + 50}
    target_soc = soc_max
    battery_charge_power_curve = {100: 0.15, 99: 0.15, 98: 0.23, 97: 0.3, 96: 0.42, 95: 0.49, 94: 0.55, 93: 0.69, 92: 0.79, 91: 0.89, 90: 0.96}
    set_charge_low_power = True
    charge_low_power_margin = my_predbat.charge_low_power_margin
    battery_rate_min = 0
    battery_rate_max_scaling = 1
    battery_loss = 0.96
    battery_temperature = 17.0
    battery_temperature_curve = {19: 0.33, 18: 0.33, 17: 0.33, 16: 0.33, 15: 0.33, 14: 0.33, 13: 0.33, 12: 0.33, 11: 0.33, 10: 0.25, 9: 0.25, 8: 0.25, 7: 0.25, 6: 0.25, 5: 0.25, 4: 0.25, 3: 0.25, 2: 0.25, 1: 0.15, 0: 0.00}
    max_rate = 2500

    best_rate, best_rate_real = find_charge_rate(
        minutes_now,
        soc,
        window,
        target_soc,
        max_rate / MINUTE_WATT,
        soc_max,
        battery_charge_power_curve,
        set_charge_low_power,
        charge_low_power_margin,
        battery_rate_min / MINUTE_WATT,
        battery_rate_max_scaling,
        battery_loss,
        log_to,
        battery_temperature=battery_temperature,
        battery_temperature_curve=battery_temperature_curve,
        current_charge_rate=current_charge_rate / MINUTE_WATT,
    )
    print("Best_rate {} Best_rate_real {}".format(best_rate * MINUTE_WATT, best_rate_real * MINUTE_WATT))
    if best_rate * MINUTE_WATT != 2500:
        print("**** ERROR: Best rate should be 2500 ****")
        failed = 1
    if best_rate_real * MINUTE_WATT != 1225:
        print("**** ERROR: Best real rate should be 1225 ****")
        failed = 1
    return failed


def test_energydataservice(my_predbat):
    """
    Test the energy data service
    """
    failed = 0

    print("Test energy data service")

    date_yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    data_example = f"""
state: 1.34
state_class: total
current_price: 1.2706032
unit: kWh
currency: EUR
region: Finland
region_code: FI
tomorrow_valid: true
next_data_update: 13:39:54
today: 1.242, 1.242, 1.242, 1.242, 1.243, 1.243, 1.243, 1.243, 1.243, 1.243, 1.244, 1.245, 1.246, 1.246, 1.261, 1.271, 1.286, 1.295, 1.295, 1.288, 1.296, 1.284, 1.273, 1.289
tomorrow: 1.284, 1.273, 1.263, 1.283, 1.309, 1.333, 1.385, 1.389, 1.366, 1.336, 1.323, 1.316, 1.318, 1.338, 1.338, 1.314, 1.299, 1.299, 1.297, 1.286, 1.281, 1.277, 1.268
raw_today:
    - hour: '{today}T00:00:00+{tz_offset}:00'
      price: 1.242
    - hour: '{today}T01:00:00+{tz_offset}:00'
      price: 1.242
    - hour: '{today}T02:00:00+{tz_offset}:00'
      price: 1.242
    - hour: '{today}T03:00:00+{tz_offset}:00'
      price: 1.242
    - hour: '{today}T04:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T05:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T06:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T07:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T08:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T09:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T10:00:00+{tz_offset}:00'
      price: 1.244
    - hour: '{today}T11:00:00+{tz_offset}:00'
      price: 1.245
    - hour: '{today}T12:00:00+{tz_offset}:00'
      price: 1.246
    - hour: '{today}T13:00:00+{tz_offset}:00'
      price: 1.246
    - hour: '{today}T14:00:00+{tz_offset}:00'
      price: 1.261
    - hour: '{today}T15:00:00+{tz_offset}:00'
      price: 1.271
    - hour: '{today}T16:00:00+{tz_offset}:00'
      price: 1.286
    - hour: '{today}T17:00:00+{tz_offset}:00'
      price: 1.295
    - hour: '{today}T18:00:00+{tz_offset}:00'
      price: 1.295
    - hour: '{today}T19:00:00+{tz_offset}:00'
      price: 1.288
    - hour: '{today}T20:00:00+{tz_offset}:00'
      price: 1.296
    - hour: '{today}T21:00:00+{tz_offset}:00'
      price: 1.284
    - hour: '{today}T22:00:00+{tz_offset}:00'
      price: 1.273
    - hour: '{today}T23:00:00+{tz_offset}:00'
      price: 1.289
raw_tomorrow:
    - hour: '{tomorrow}T00:00:00+{tz_offset}:00'
      price: 1.284
    - hour: '{tomorrow}T01:00:00+{tz_offset}:00'
      price: 1.273
    - hour: '{tomorrow}T02:00:00+{tz_offset}:00'
      price: 1.263
    - hour: '{tomorrow}T03:00:00+{tz_offset}:00'
      price: 1.283
    - hour: '{tomorrow}T04:00:00+{tz_offset}:00'
      price: 1.309
    - hour: '{tomorrow}T05:00:00+{tz_offset}:00'
      price: 1.333
    - hour: '{tomorrow}T06:00:00+{tz_offset}:00'
      price: 1.385
    - hour: '{tomorrow}T07:00:00+{tz_offset}:00'
      price: 1.389
    - hour: '{tomorrow}T08:00:00+{tz_offset}:00'
      price: 1.366
    - hour: '{tomorrow}T09:00:00+{tz_offset}:00'
      price: 1.336
    - hour: '{tomorrow}T10:00:00+{tz_offset}:00'
      price: 1.323
    - hour: '{tomorrow}T11:00:00+{tz_offset}:00'
      price: 1.316
    - hour: '{tomorrow}T12:00:00+{tz_offset}:00'
      price: 1.318
    - hour: '{tomorrow}T13:00:00+{tz_offset}:00'
      price: 1.338
    - hour: '{tomorrow}T14:00:00+{tz_offset}:00'
      price: 1.338
    - hour: '{tomorrow}T15:00:00+{tz_offset}:00'
      price: 1.314
    - hour: '{tomorrow}T16:00:00+{tz_offset}:00'
      price: 1.299
    - hour: '{tomorrow}T17:00:00+{tz_offset}:00'
      price: 1.299
    - hour: '{tomorrow}T18:00:00+{tz_offset}:00'
      price: 1.297
    - hour: '{tomorrow}T19:00:00+{tz_offset}:00'
      price: 1.286
    - hour: '{tomorrow}T20:00:00+{tz_offset}:00'
      price: 1.281
    - hour: '{tomorrow}T21:00:00+{tz_offset}:00'
      price: 1.277
    - hour: '{tomorrow}T22:00:00+{tz_offset}:00'
      price: 1.268
    - hour: '{tomorrow}T23:00:00+{tz_offset}:00'
      price: 1.268
today_min:
    hour: '{today}T00:00:00+{tz_offset}:00'
    price: 1.242
today_max:
    hour: '{today}T20:00:00+{tz_offset}:00'
    price: 1.296
today_mean: 1.26
tomorrow_min:
    hour: '{tomorrow}T02:00:00+{tz_offset}:00'
    price: 1.263
tomorrow_max:
    hour: '{tomorrow}T07:00:00+{tz_offset}:00'
    price: 1.389
tomorrow_mean: 1.312
use_cent: false
attribution: Data sourced from Nord Pool
unit_of_measurement: EUR/kWh
device_class: monetary
icon: mdi:flash
friendly_name: Energi Data Service
"""

    ha = my_predbat.ha_interface
    ha.dummy_items["sensor.energi_data_service"] = yaml.safe_load(data_example)
    my_predbat.args["energi_data_service"] = "sensor.energi_data_service"
    rates = my_predbat.fetch_energidataservice_rates("sensor.energi_data_service")

    show = []
    for minute in range(0, 48 * 60, 15):
        show.append(rates[minute])

    expected_show = [
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.4,
        124.4,
        124.4,
        124.4,
        124.5,
        124.5,
        124.5,
        124.5,
        124.6,
        124.6,
        124.6,
        124.6,
        124.6,
        124.6,
        124.6,
        124.6,
        126.1,
        126.1,
        126.1,
        126.1,
        127.1,
        127.1,
        127.1,
        127.1,
        128.6,
        128.6,
        128.6,
        128.6,
        129.5,
        129.5,
        129.5,
        129.5,
        129.5,
        129.5,
        129.5,
        129.5,
        128.8,
        128.8,
        128.8,
        128.8,
        129.6,
        129.6,
        129.6,
        129.6,
        128.4,
        128.4,
        128.4,
        128.4,
        127.3,
        127.3,
        127.3,
        127.3,
        128.9,
        128.9,
        128.9,
        128.9,
        128.4,
        128.4,
        128.4,
        128.4,
        127.3,
        127.3,
        127.3,
        127.3,
        126.3,
        126.3,
        126.3,
        126.3,
        128.3,
        128.3,
        128.3,
        128.3,
        130.9,
        130.9,
        130.9,
        130.9,
        133.3,
        133.3,
        133.3,
        133.3,
        138.5,
        138.5,
        138.5,
        138.5,
        138.9,
        138.9,
        138.9,
        138.9,
        136.6,
        136.6,
        136.6,
        136.6,
        133.6,
        133.6,
        133.6,
        133.6,
        132.3,
        132.3,
        132.3,
        132.3,
        131.6,
        131.6,
        131.6,
        131.6,
        131.8,
        131.8,
        131.8,
        131.8,
        133.8,
        133.8,
        133.8,
        133.8,
        133.8,
        133.8,
        133.8,
        133.8,
        131.4,
        131.4,
        131.4,
        131.4,
        129.9,
        129.9,
        129.9,
        129.9,
        129.9,
        129.9,
        129.9,
        129.9,
        129.7,
        129.7,
        129.7,
        129.7,
        128.6,
        128.6,
        128.6,
        128.6,
        128.1,
        128.1,
        128.1,
        128.1,
        127.7,
        127.7,
        127.7,
        127.7,
        126.8,
        126.8,
        126.8,
        126.8,
        126.8,
        126.8,
        126.8,
        126.8,
    ]

    if json.dumps(show) != json.dumps(expected_show):
        print("ERROR: Expecting show should be:\n {} got:\n {}".format(expected_show, show))
        failed = 1

    return failed


def test_alert_feed(my_predbat):
    """
    Test the alert feed
    """
    failed = 0
    ha = my_predbat.ha_interface
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    birmingham = [52.4823, -1.8900]
    bristol = [51.4545, -2.5879]
    manchester = [53.4808, -2.2426]
    fife = [56.2082, -3.1495]

    alert_data = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
  <link href="https://pubsubhubbub.appspot.com/" rel="hub"/>
  <link href="https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom" rel="self" type="application/atom+xml"/>
  <link href="https://meteoalarm.org" rel="alternate" type="text/html"/>
  <rights>Copyright © 2025 MeteoAlarm.Org. Licensed under terms equivalent to CC BY 4.0, with additional requirements for redistributing outlined in our Terms and Conditions.</rights>
  <generator>MeteoAlarm Producer Server</generator>
  <logo>https://feeds.meteoalarm.org/images/logo.svg</logo>
  <author>
    <name>meteoalarm.org</name>
    <uri>https://meteoalarm.org</uri>
    <email>meteoalarm@geosphere.at</email>
  </author>
  <id>tag:meteoalarm.org,2021-02-19:UK</id>
  <title>MeteoAlarm - Alerting Europe for Extreme Weather</title>
  <updated>2025-01-24T18:07:55.906349Z</updated>
  <entry>
    <cap:polygon>56.3439,-7.4487 55.9892,-7.1082 55.5659,-6.8445 55.3573,-6.8774 55.2572,-7.0367 55.1569,-7.1466 55.1161,-7.3718 55.0768,-7.4625 55.0217,-7.5064 54.9287,-7.5586 54.8402,-7.6245 54.7991,-7.6959 54.7959,-7.8113 54.788,-7.9019 54.7595,-7.9623 54.7183,-7.9926 54.6659,-7.9926 54.6246,-7.9953 54.4956,-7.207 54.4892,-6.4929 54.355,-5.7623 54.0658,-4.8999 53.5468,-3.1311 53.7032,-2.2302 53.927,-1.7523 54.2396,-0.5383 54.3742,-0.2307 54.4956,-0.2637 54.6421,-0.4724 54.8133,-0.8459 55.0091,-1.0547 55.3541,-1.1865 55.6528,-1.2964 55.8691,-1.4502 56.1149,-1.8457 56.3043,-2.0215 56.4989,-2.0544 56.8009,-1.9885 57.0766,-1.8018 57.3087,-1.582 57.5099,-1.5271 57.6454,-1.571 57.7687,-1.7084 57.8097,-1.9336 57.8184,-2.2028 57.7921,-2.7081 57.7921,-3.0817 57.8331,-3.3069 57.8973,-3.4277 57.9732,-3.4662 58.0692,-3.3838 58.2546,-3.0267 58.5224,-2.6093 58.9273,-1.9995 59.3612,-1.3623 59.7841,-0.835 60.1634,-0.5328 60.6301,-0.3571 60.8155,-0.401 60.9224,-0.5328 60.9758,-0.7471 60.9758,-1.1096 60.8556,-1.5875 60.6058,-2.1094 60.3568,-2.428 59.7896,-3.0322 59.322,-3.6584 59.0179,-4.4495 58.8535,-5.4822 58.6826,-6.5479 58.4937,-7.2729 58.3153,-7.6904 58.0023,-8.053 57.5571,-8.2288 57.172,-8.1738 56.903,-8.0859 56.5353,-7.6904 56.3439,-7.4487</cap:polygon>
    <link href="https://meteoalarm.org?polygon=3ec5a08b-995f-45fc-88eb-d364b1613e41,0,0,0" hreflang="en" title="Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | North East England | North West England | Northern Ireland | Orkney &amp; Shetland | Strathclyde | SW Scotland, Lothian Borders | Yorkshire &amp; Humber"/>
    <cap:areaDesc>Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | North East England | North West England | Northern Ireland | Orkney &amp; Shetland | Strathclyde | SW Scotland, Lothian Borders | Yorkshire &amp; Humber</cap:areaDesc>
    <cap:event>Yellow wind warning</cap:event>
    <cap:sent>2025-01-24T18:01:26+{tz_offset}:00</cap:sent>
    <cap:expires>{today}T23:59:59+{tz_offset}:00</cap:expires>
    <cap:effective>{yesterday}T10:40:36+{tz_offset}:00</cap:effective>
    <cap:onset>{today}T00:00:00+{tz_offset}:00</cap:onset>
    <cap:certainty>Possible</cap:certainty>
    <cap:severity>Moderate</cap:severity>
    <cap:urgency>Immediate</cap:urgency>
    <cap:scope>Public</cap:scope>
    <cap:message_type>Update</cap:message_type>
    <cap:status>Actual</cap:status>
    <cap:identifier>2.49.0.0.826.0.GB_250124180126_cecc0a37.v6.0.W</cap:identifier>
    <link href="https://feeds.meteoalarm.org/api/v1/warnings/feeds-united-kingdom/3ec5a08b-995f-45fc-88eb-d364b1613e41" type="application/cap+xml"/>
    <link href="https://meteoalarm.org?region=UK" hreflang="en" rel="related" title="United Kingdom"/>
    <author>
      <name>meteoalarm.org</name>
      <uri>https://meteoalarm.org</uri>
    </author>
    <published>2025-01-24T18:01:26Z</published>
    <id>https://feeds.meteoalarm.org/api/v1/warnings/feeds-united-kingdom/3ec5a08b-995f-45fc-88eb-d364b1613e41?index_info=0&amp;index_area=0&amp;index_polygon=0</id>
    <title>Yellow Wind Warning issued for United Kingdom - Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | North East England | North West England | Northern Ireland | Orkney &amp; Shetland | Strathclyde | SW Scotland, Lothian Borders | Yorkshire &amp; Humber</title>
    <updated>2025-01-24T18:01:26Z</updated>
  </entry>
<entry>
    <cap:polygon>60.877,-1.4282 59.4674,-3.23 59.249,-3.7573 58.95,-4.4275 58.9103,-4.8395 58.4618,-6.7053 57.7687,-7.8949 56.7768,-7.8333 56.7407,-7.3059 56.7557,-6.7841 56.7768,-6.4545 56.7286,-6.1908 56.6139,-5.9601 56.4534,-5.5872 56.3653,-5.4108 56.2769,-5.1306 56.2586,-5.0153 56.2403,-4.8395 56.2189,-4.3671 56.222,-4.2462 56.2525,-4.0869 56.2678,-3.8892 56.283,-3.8068 56.3835,-3.4113 56.5776,-2.9169 56.6562,-2.774 56.6894,-2.7301 56.8099,-2.3816 56.8079,-2.1936 57.1184,-1.8896 57.4509,-1.593 57.7218,-1.8018 57.7628,-3.3838 57.9906,-3.6035 58.3499,-2.9114 59.2996,-1.4612 60.5222,-0.4834 60.9411,-0.7031 60.877,-1.4282</cap:polygon>
    <link href="https://meteoalarm.org?polygon=05f2a1ec-58ec-4b6e-b05b-21ddac714680,0,0,0" hreflang="en" title="Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | Orkney &amp; Shetland | Strathclyde"/>
    <cap:areaDesc>Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | Orkney &amp; Shetland | Strathclyde</cap:areaDesc>
    <cap:event>Amber wind warning</cap:event>
    <cap:sent>2025-01-23T10:42:05+{tz_offset}:00</cap:sent>
    <cap:expires>{tomorrow}T06:00:00+{tz_offset}:00</cap:expires>
    <cap:effective>{yesterday}T10:42:05+{tz_offset}:00</cap:effective>
    <cap:onset>{today}T13:00:00+{tz_offset}:00</cap:onset>
    <cap:certainty>Likely</cap:certainty>
    <cap:severity>Severe</cap:severity>
    <cap:urgency>Future</cap:urgency>
    <cap:scope>Public</cap:scope>
    <cap:message_type>Alert</cap:message_type>
    <cap:status>Actual</cap:status>
    <cap:identifier>2.49.0.0.826.0.GB_250123104205_72085267.v1.0.W</cap:identifier>
    <link href="https://feeds.meteoalarm.org/api/v1/warnings/feeds-united-kingdom/05f2a1ec-58ec-4b6e-b05b-21ddac714680" type="application/cap+xml"/>
    <link href="https://meteoalarm.org?region=UK" hreflang="en" rel="related" title="United Kingdom"/>
    <author>
      <name>meteoalarm.org</name>
      <uri>https://meteoalarm.org</uri>
    </author>
    <published>2025-01-23T10:42:05Z</published>
    <id>https://feeds.meteoalarm.org/api/v1/warnings/feeds-united-kingdom/05f2a1ec-58ec-4b6e-b05b-21ddac714680?index_info=0&amp;index_area=0&amp;index_polygon=0</id>
    <title>Orange Wind Warning issued for United Kingdom - Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | Orkney &amp; Shetland | Strathclyde</title>
    <updated>2025-01-23T10:42:05Z</updated>
  </entry>
</feed>
"""
    print("Test alert feed")

    result = my_predbat.parse_alert_data(alert_data)
    if not result:
        print("ERROR: Could not parse stored alert data")
        failed = 1
        return failed

    filter = my_predbat.filter_alerts(result, area="North West England")
    if len(filter) != 1:
        print("ERROR: Expecting 1 alert for North West England got {}".format(len(filter)))
        failed = 1
        return failed

    filter = my_predbat.filter_alerts(result, area="South West England")
    if len(filter) != 0:
        print("ERROR: Expecting 0 alert for South West England got {}".format(len(filter)))
        failed = 1
        return failed

    filter = my_predbat.filter_alerts(result, latitude=birmingham[0], longitude=birmingham[1])
    if len(filter) != 0:
        print("ERROR: Expecting 0 alert for Birmingham got {}".format(len(filter)))
        failed = 1
        return failed

    filter = my_predbat.filter_alerts(result, latitude=fife[0], longitude=fife[1])
    if len(filter) != 1:
        print("ERROR: Expecting 1 alert for Fife got {}".format(len(filter)))
        failed = 1
        return failed

    filter = my_predbat.filter_alerts(result, area="Grampian", severity="Moderate|Severe", certainty="Likely")
    if len(filter) != 1:
        print("ERROR: Expecting 1 alert for Grampian got {}".format(len(filter)))
        failed = 1
        return failed

    filter = my_predbat.filter_alerts(result, event="(Amber|Yellow|Orange|Red).*(Wind|Snow|Fog|Thunderstorm|Avalanche|Frost|Heat|Coastal event|Flood|Forestfire|Ice|Low temperature|Storm|Tornado|Tsunami|Volcano|Wildfire)")
    if len(filter) != 2:
        print("ERROR: Expecting 2 alerts for Yellow|Amber but got {}".format(len(filter)))
        failed = 1
        return failed

    alert_active_keep = my_predbat.apply_alerts(result, 1.0)
    show = []
    for minute in range(0, 48 * 60, 15):
        show.append(alert_active_keep.get(minute, 0))
    expect_show = [
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    ]
    if json.dumps(show) != json.dumps(expect_show):
        print("ERROR: Expecting show should be {} got {}".format(expect_show, show))
        failed = 1

    url = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom"
    xml = my_predbat.download_alert_data(url)
    if not xml:
        print("ERROR: Could not download alert data")
        failed = 1
        return failed

    alert_config = {
        "url": url,
        "area": "North West England",
        "event": "Yellow|Amber",
        "keep": 0.5,
    }
    original_download_alert_data = my_predbat.download_alert_data
    my_predbat.download_alert_data = MagicMock(return_value=alert_data)
    my_predbat.args["alerts"] = alert_config
    my_predbat.process_alerts()
    my_predbat.download_alert_data = original_download_alert_data
    alert_active_keep = my_predbat.alert_active_keep
    my_predbat.alert_active_keep = {}
    show = []
    for minute in range(0, 48 * 60, 15):
        show.append(alert_active_keep.get(minute, 0))

    expect_show = [
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    ]
    if json.dumps(show) != json.dumps(expect_show):
        print("ERROR: Expecting show should be {} got {}".format(expect_show, show))
        failed = 1

    alert_text = ha.get_state(my_predbat.prefix + ".alerts")
    expect_text = "Yellow wind warning until " + today + " 23:59:59+{}:00".format(tz_offset)
    if alert_text != expect_text:
        print("ERROR: Expecting alert text to be '{}' got '{}'".format(expect_text, alert_text))
        failed = 1

    return failed


def test_saving_session(my_predbat):
    """
    Test the octopus saving session
    """
    print("Test saving session")
    ha = my_predbat.ha_interface
    failed = False
    date_last_year = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    date_yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    date_before_yesterday = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    date_today = datetime.now().strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"
    session_binary = f"""

state: off
current_joined_event_start: '{date_today}T16:30:00+{tz_offset}:00'
current_joined_event_end: '{date_today}T17:30:00+{tz_offset}:00'
current_joined_event_duration_in_minutes: 60
next_joined_event_start: null
next_joined_event_end: null
next_joined_event_duration_in_minutes: null
icon: mdi:leaf
friendly_name: Octoplus Saving Session (A-4DD6C5EE)
""".format(
        date_last_year=date_last_year, date_yesterday=date_yesterday, date_today=date_today, date_before_yesterday=date_before_yesterday, tz_offset=tz_offset
    )

    session_sensor = f"""
state: '2025-01-23T12:10:11.108+{tz_offset}:00'
event_types: octopus_energy_all_octoplus_saving_sessions
event_type: octopus_energy_all_octoplus_saving_sessions
account_id: A-4DD6C5EE
available_events:
    - id: 1336
      start: '{date_today}T18:30:00+{tz_offset}:00'
      end: '{date_today}T19:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 500
      code: 987654
joined_events:
    - id: 1327
      start: '{date_last_year}T17:00:00+{tz_offset}:00'
      end: '{date_last_year}T18:00:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: 936
      octopoints_per_kwh: 576
    - id: 1334
      start: '{date_yesterday}T17:30:00+{tz_offset}:00'
      end: '{date_yesterday}T18:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 192
    - id: 1335
      start: '{date_today}T16:30:00+{tz_offset}:00'
      end: '{date_today}T17:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 448
    - id: 1336
      start: '{date_before_yesterday}T23:30:00+{tz_offset}:00'
      end: '{date_yesterday}T10:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 448
friendly_name: Octoplus Saving Session Events (A-12345678)
""".format(
        date_last_year=date_last_year, date_yesterday=date_yesterday, date_today=date_today, tz_offset=tz_offset
    )
    ha.dummy_items["binary_sensor.octopus_energy_a_12345678_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
    ha.dummy_items["event.octopus_energy_a_12345678_octoplus_saving_session_events"] = yaml.safe_load(session_sensor)
    ha.dummy_items["sensor.octopus_free_session"] = {}
    my_predbat.args["octopus_saving_session"] = "binary_sensor.octopus_energy_a_12345678_octoplus_saving_sessions"
    my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
    if "octopus_free_url" in my_predbat.args:
        del my_predbat.args["octopus_free_url"]
    my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 10

    ha.service_store_enable = True
    octopus_free_slots, octopus_saving_slots = my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    expected_saving = [
        {"start": "{}T17:30:00+{}:00".format(date_yesterday, tz_offset), "end": "{}T18:30:00+{}:00".format(date_yesterday, tz_offset), "rate": 19.2, "state": False},
        {"start": "{}T16:30:00+{}:00".format(date_today, tz_offset), "end": "{}T17:30:00+{}:00".format(date_today, tz_offset), "rate": 44.8, "state": False},
        {"start": "{}T23:30:00+{}:00".format(date_before_yesterday, tz_offset), "end": "{}T10:30:00+{}:00".format(date_yesterday, tz_offset), "rate": 44.8, "state": False},
    ]

    # Example format Sat 25/01
    date_today_service = datetime.now().strftime("%a %d/%m")
    expected_service = [
        ["octopus_energy/join_octoplus_saving_session_event", {"event_code": 987654, "entity_id": "event.octopus_energy_a_12345678_octoplus_saving_session_events"}],
        ["notify/notify", {"message": "Predbat: Joined Octopus saving event {} 18:30-19:30, 50.0 p/kWh".format(date_today_service)}],
    ]

    if json.dumps(octopus_saving_slots) != json.dumps(expected_saving):
        print("ERROR: Expecting saving slots should be {} got {}".format(expected_saving, octopus_saving_slots))
        failed = 1
    if json.dumps(service_result) != json.dumps(expected_service):
        print("ERROR: Expecting service store should be {} got {}".format(expected_service, service_result))
        failed = 1
    if octopus_free_slots:
        print("ERROR: Expecting no free slots")
        failed = 1

    rate_import_replicated = {}
    my_predbat.rate_import = {n: 0 for n in range(-24 * 60, 48 * 60)}
    my_predbat.load_saving_slot(expected_saving, export=False, rate_replicate=rate_import_replicated)
    price_ranges = [[(17.5 - 24) * 60, (18.5 - 24) * 60, 19.2], [(16.5) * 60, (17.5) * 60, 44.8], [-24 * 60, (10.5 - 24) * 60, 44.8]]
    for minute in range(-24 * 60, 48 * 60):
        rate = my_predbat.rate_import[minute]
        in_range = False
        for price_range in price_ranges:
            if minute >= price_range[0] and minute < price_range[1]:
                if rate != price_range[2]:
                    print("ERROR: Load Octopus Saving - minute {} Expecting rate to be {} got {}".format(minute, price_range[2], rate))
                    failed = 1
                    break
                in_range = True
        if not in_range:
            if rate != 0:
                print("ERROR: Load Octopus Saving - minute {} Expecting rate to be 0 got {}".format(minute, rate))
                failed = 1
                break

    return failed


def run_test_octopus_api(my_predbat, octopus_api, octopus_account):
    """
    Run the Octopus API tests
    """
    print("Test Octopus API")
    failed = False

    octopus_api = OctopusAPI(octopus_api, octopus_account, my_predbat)
    my_predbat.octopus_api_direct = octopus_api
    my_predbat.create_task(octopus_api.start())
    octopus_api.wait_api_started()

    planned_dispatches = octopus_api.get_intelligent_planned_dispatches()
    completed_dispatches = octopus_api.get_intelligent_completed_dispatches()
    vehicle = octopus_api.get_intelligent_vehicle()
    available_events, joined_events = octopus_api.get_saving_session_data()
    print("Planned dispatches: {}".format(planned_dispatches))
    print("Completed dispatches: {}".format(completed_dispatches))
    print("Vehicle: {}".format(vehicle))
    print("Saving session available {}".format(available_events))
    print("Saving session joined {}".format(joined_events))
    octopus_api.join_saving_session_event("EVENT_3_210125")
    time.sleep(10)
    octopus_api.stop()
    time.sleep(1)

    failed = 1
    return failed


def run_test_manual_api(my_predbat):
    failed = 0
    print("Test manual API")

    # Reset
    my_predbat.api_select("manual_api", "")
    original_limit = my_predbat.args["inverter_limit"]

    my_predbat.args["inverter_limit"] = [3600, 3500]
    my_predbat.args["inverter_limit_charge"] = [3600, 3600]
    limit = my_predbat.get_arg("inverter_limit", 0, index=0)
    if limit != 3600:
        print("ERROR: T1 Expecting inverter limit 0 to be 3600 got {}".format(limit))
        failed = 1
    limit = my_predbat.get_arg("inverter_limit", 0, index=1)
    if limit != 3500:
        print("ERROR: T2 Expecting inverter limit 0 to be 3500 got {}".format(limit))
        failed = 1
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [3600, 3500]
    if limits != expected:
        print("ERROR: T3 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit=1000")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")

    limit = my_predbat.get_arg("inverter_limit", 0, index=0)
    expected = 1000
    if limit != expected:
        print("ERROR: T4 Expecting inverter limit 0 to be {} got {}".format(expected, limit))
        failed = 1

    limit = my_predbat.get_arg("inverter_limit", 0, index=1)
    expected = 3500
    if limit != expected:
        print("ERROR: T5 Expecting inverter limit 0 to be {} got {}".format(expected, limit))
        failed = 1

    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [1000, 3500]
    if limits != expected:
        print("ERROR: T6 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "[inverter_limit=1000]")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [3600, 3500]
    if limits != expected:
        print("ERROR: T7 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit(1)=1000")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [3600, 1000]
    if limits != expected:
        print("ERROR: T8 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit(0)=900")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [900, 1000]
    if limits != expected:
        print("ERROR: T8 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit(0)=800")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [800, 1000]
    if limits != expected:
        print("ERROR: T9 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "off")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [3600, 3500]
    if limits != expected:
        print("ERROR: T3 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.args["inverter_limit"] = original_limit
    my_predbat.args["rates_export_override"] = []

    export_override = my_predbat.get_arg("rates_export_override", [])
    if export_override != []:
        print("ERROR: T10 Expecting rate export override to be {} got {}".format([], export_override))
        failed = 1

    my_predbat.api_select("manual_api", "rates_export_override?start=17:00:00&end=19:00:00&rate=0")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    export_override = my_predbat.get_arg("rates_export_override", [])
    expected = [{"start": "17:00:00", "end": "19:00:00", "rate": "0"}]
    if export_override != expected:
        print("ERROR: T11 Expecting rate export override to be {} got {}".format(expected, export_override))
        failed = 1

    my_predbat.api_select("manual_api", "rates_export_override(1)?start=12:00:00&end=13:00:00&rate=2")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    export_override = my_predbat.get_arg("rates_export_override", [])
    expected = [{"start": "17:00:00", "end": "19:00:00", "rate": "0"}, {"start": "12:00:00", "end": "13:00:00", "rate": "2"}]
    if export_override != expected:
        print("ERROR: T12 Expecting rate export override to be {} got {}".format(expected, export_override))
        failed = 1

    my_predbat.api_select("manual_api", "off")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    export_override = my_predbat.get_arg("rates_export_override", [])
    expected = []
    if export_override != expected:
        print("ERROR: T13 Expecting rate export override to be {} got {}".format(expected, export_override))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit_charge(0)=800")
    my_predbat.api_select("manual_api", "inverter_limit_charge(1)=400")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit_charge", [])
    expected = [800, 400]
    if limits != expected:
        print("ERROR: T14 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1
    limit0 = my_predbat.get_arg("inverter_limit_charge", index=0, default=0)
    if limit0 != 800:
        print("ERROR: T15 Expecting inverter limit 0 to be {} got {}".format(800, limit0))
        failed = 1
    limit1 = my_predbat.get_arg("inverter_limit_charge", index=1, default=0)
    if limit1 != 400:
        print("ERROR: T16 Expecting inverter limit 1 to be {} got {}".format(400, limit1))
        failed = 1

    del my_predbat.args["inverter_limit_charge"]

    return failed


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Predbat unit tests")
    parser.add_argument("--debug_file", action="store", help="Enable debug output")
    parser.add_argument("--quick", action="store_true", help="Run quick tests")
    parser.add_argument("--compare", action="store_true", help="Run compare")
    parser.add_argument("--gecloud", action="store_true", help="Run tests for GivEnergy Cloud")
    parser.add_argument("--octopus_api", action="store", help="Run Octopus API tests with given token")
    parser.add_argument("--octopus_account", action="store", help="Octopus API account ID")
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
        run_single_debug(args.debug_file, my_predbat, args.debug_file, compare=args.compare)
        sys.exit(0)

    if not failed and args.gecloud:
        failed |= run_test_ge_cloud(my_predbat)
        return failed

    if not failed and args.octopus_api:
        failed |= run_test_octopus_api(my_predbat, args.octopus_api, args.octopus_account)
        return failed

    if not failed:
        failed |= run_test_manual_api(my_predbat)
    if not failed:
        failed |= run_test_web_if(my_predbat)
    if not failed:
        failed |= run_nordpool_test(my_predbat)
    if not failed:
        failed |= run_load_octopus_slots_tests(my_predbat)
    if not failed:
        failed |= test_basic_rates(my_predbat)
    if not failed:
        failed |= test_find_charge_rate(my_predbat)
    if not failed:
        failed |= test_energydataservice(my_predbat)
    if not failed:
        failed |= test_saving_session(my_predbat)
    free_sessions = my_predbat.download_octopus_free("http://octopus.energy/free-electricity")
    free_sessions = my_predbat.download_octopus_free("http://octopus.energy/free-electricity")
    if not free_sessions:
        print("**** ERROR: No free sessions found ****")
        failed = 1
    if not failed:
        failed |= test_alert_feed(my_predbat)
    if not failed:
        failed |= run_inverter_tests()
    if not failed:
        failed |= run_car_charging_smart_tests(my_predbat)
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
    if not failed and not args.quick:
        failed |= run_perf_test(my_predbat)

    if not failed and not args.quick:
        # Scan .yaml files in cases directory
        for filename in glob.glob("cases/*.yaml"):
            basename = os.path.basename(filename)
            pathname = os.path.dirname(filename)
            failed |= run_single_debug(basename, my_predbat, filename, pathname + "/" + basename + ".expected.json")
            if failed:
                break

    if failed:
        print("**** ERROR: Some tests failed ****")
        sys.exit(1)
    print("**** Tests passed ****")
    sys.exit(0)


if __name__ == "__main__":
    main()
