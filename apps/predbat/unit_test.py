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
from utils import calc_percent_limit, remove_intersecting_windows
from futurerate import FutureRate
from config import PREDICT_STEP, MINUTE_WATT
from inverter import Inverter
from config import INVERTER_DEF

KEEP_SCALE = 0.5


class TestHAInterface:
    def __init__(self):
        self.step = 5
        self.build_history()
        self.history_enable = True
        self.dummy_items = {}
        self.service_store_enable = False
        self.service_store = []

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
            print("Getting state: {} {}".format(entity_id, self.dummy_items[entity_id]))
            return self.dummy_items[entity_id]
        else:
            return None

    def call_service(self, service, **kwargs):
        # print("Calling service: {} {}".format(service, kwargs))
        if self.service_store_enable:
            self.service_store.append([service, kwargs])
            return None

        if service == "number/set_value":
            entity_id = kwargs.get("entity_id", None)
            if not entity_id.startswith("number."):
                print("Warn: Service for entity {} not a number".format(entity_id))
            elif entity_id in self.dummy_items:
                self.dummy_items[entity_id] = kwargs.get("value", 0)
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
        # print("Setting state: {} to {}".format(entity_id, state))
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

    def dummy_rest_postCommand(self, url, json):
        """
        Dummy rest post command
        """
        # print("Dummy rest post command {} {}".format(url, json))
        self.commands.append([url, json])

    def dummy_rest_getData(self, url):
        if url == "dummy/runAll":
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


def test_adjust_charge_window(test_name, ha, inv, dummy_rest, prev_charge_start_time, prev_charge_end_time, prev_enable_charge, charge_start_time, charge_end_time, minutes_now):
    """
    test:
        inv.adjust_charge_window(self, charge_start_time, charge_end_time, minutes_now):
    """
    failed = False
    print("Test: {}".format(test_name))

    inv.rest_data = None
    ha.dummy_items["select.charge_start_time"] = prev_charge_start_time
    ha.dummy_items["select.charge_end_time"] = prev_charge_end_time
    ha.dummy_items["switch.scheduled_charge_enable"] = "on" if prev_enable_charge else "off"
    charge_start_time_tm = datetime.strptime(charge_start_time, "%H:%M:%S")
    charge_end_time_tm = datetime.strptime(charge_end_time, "%H:%M:%S")

    inv.adjust_charge_window(charge_start_time_tm, charge_end_time_tm, minutes_now)
    if ha.get_state("select.charge_start_time") != charge_start_time:
        print("ERROR: Charge start time should be {} got {}".format(charge_start_time, ha.get_state("select.charge_start_time")))
        failed = True
    if ha.get_state("select.charge_end_time") != charge_end_time:
        print("ERROR: Charge end time should be {} got {}".format(charge_end_time, ha.get_state("select.charge_end_time")))
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


def test_adjust_charge_rate(test_name, ha, inv, dummy_rest, prev_rate, rate, expect_rate=None, discharge=False):
    """
    Test the adjust_inverter_mode function
    """
    failed = False
    if expect_rate is None:
        expect_rate = rate

    print("Test: {}".format(test_name))

    # Non-REST Mode
    inv.rest_data = None
    entity = "number.discharge_rate" if discharge else "number.charge_rate"
    ha.dummy_items[entity] = prev_rate
    if discharge:
        inv.adjust_discharge_rate(rate)
    else:
        inv.adjust_charge_rate(rate)
    if ha.get_state(entity) != expect_rate:
        print("ERROR: Inverter rate should be {} got {}".format(expect_rate, ha.get_state(entity)))
        failed = True

    # REST Mode
    rest_entity = "Battery_Discharge_Rate" if discharge else "Battery_Charge_Rate"
    inv.rest_api = "dummy"
    inv.rest_data = {}
    inv.rest_data["Control"] = {}
    inv.rest_data["Control"][rest_entity] = prev_rate
    dummy_rest.rest_data = copy.deepcopy(inv.rest_data)
    dummy_rest.rest_data["Control"][rest_entity] = expect_rate

    if discharge:
        inv.adjust_discharge_rate(rate)
    else:
        inv.adjust_charge_rate(rate)

    rest_command = dummy_rest.get_commands()
    if prev_rate != expect_rate:
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
    my_predbat.args["device_id"] = "DID0"

    dummy_items["select.charge_start_time"] = charge_start_time
    dummy_items["select.charge_end_time"] = charge_end_time

    power = int(inv.battery_rate_max_charge * MINUTE_WATT)

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

    time_now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    dummy_items = {
        "number.charge_rate": 1100,
        "number.discharge_rate": 1500,
        "number.charge_limit": 100,
        "select.pause_mode": "Disabled",
        "sensor.battery_capacity": 10.0,
        "sensor.battery_soc": 0.0,
        "sensor.soc_max": 10.0,
        "sensor.soc_kw": 1.0,
        "select.inverter_mode": "eco",
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

    failed |= test_adjust_battery_target("adjust_target50", ha, inv, dummy_rest, 0, 50, True, False, 50)
    failed |= test_adjust_battery_target("adjust_target0", ha, inv, dummy_rest, 10, 0, True, False, 4)
    failed |= test_adjust_battery_target("adjust_target100", ha, inv, dummy_rest, 99, 100, True, False, 100)
    failed |= test_adjust_battery_target("adjust_target100r", ha, inv, dummy_rest, 100, 100, True, False, 100)

    failed |= test_adjust_inverter_mode("adjust_mode_eco1", ha, inv, dummy_rest, "Timed Export", "Eco", "Eco")
    failed |= test_adjust_inverter_mode("adjust_mode_eco2", ha, inv, dummy_rest, "Eco", "Eco", "Eco")
    failed |= test_adjust_inverter_mode("adjust_mode_eco3", ha, inv, dummy_rest, "Eco (Paused)", "Eco", "Eco (Paused)")
    failed |= test_adjust_inverter_mode("adjust_mode_export1", ha, inv, dummy_rest, "Eco (Paused)", "Timed Export", "Timed Export")
    failed |= test_adjust_inverter_mode("adjust_mode_export2", ha, inv, dummy_rest, "Timed Export", "Timed Export", "Timed Export")

    failed |= test_adjust_charge_rate("adjust_charge_rate1", ha, inv, dummy_rest, 0, 200.1, 200)
    failed |= test_adjust_charge_rate("adjust_charge_rate2", ha, inv, dummy_rest, 200, 0, 0)
    failed |= test_adjust_charge_rate("adjust_charge_rate3", ha, inv, dummy_rest, 200, 210, 200)

    failed |= test_adjust_charge_rate("adjust_discharge_rate1", ha, inv, dummy_rest, 0, 200.1, 200, discharge=True)
    failed |= test_adjust_charge_rate("adjust_discharge_rate2", ha, inv, dummy_rest, 200, 0, 0, discharge=True)
    failed |= test_adjust_charge_rate("adjust_discharge_rate3", ha, inv, dummy_rest, 200, 210, 200, discharge=True)

    failed |= test_adjust_reserve("adjust_reserve1", ha, inv, dummy_rest, 4, 50, reserve_max=100)
    failed |= test_adjust_reserve("adjust_reserve2", ha, inv, dummy_rest, 50, 0, 4, reserve_max=100)
    failed |= test_adjust_reserve("adjust_reserve3", ha, inv, dummy_rest, 20, 100, reserve_max=100)
    failed |= test_adjust_reserve("adjust_reserve4", ha, inv, dummy_rest, 20, 100, 98, reserve_min=4, reserve_max=98)
    failed |= test_adjust_reserve("adjust_reserve5", ha, inv, dummy_rest, 50, 0, 0, reserve_min=0, reserve_max=100)

    failed |= test_adjust_charge_window("adjust_charge_window1", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, "00:00:00", "00:00:00", my_predbat.minutes_now)
    failed |= test_adjust_charge_window("adjust_charge_window2", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, "00:00:00", "23:00:00", my_predbat.minutes_now)
    failed |= test_adjust_charge_window("adjust_charge_window2", ha, inv, dummy_rest, "00:00:00", "00:00:00", True, "00:00:00", "23:00:00", my_predbat.minutes_now)
    failed |= test_adjust_charge_window("adjust_charge_window3", ha, inv, dummy_rest, "00:00:00", "00:00:00", False, "01:12:00", "23:12:00", my_predbat.minutes_now)
    failed |= test_adjust_charge_window("adjust_charge_window3", ha, inv, dummy_rest, "00:00:00", "00:00:00", True, "01:12:00", "23:12:00", my_predbat.minutes_now)

    failed |= test_call_service_template("test_service_simple1", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra": "data"})
    failed |= test_call_service_template("test_service_simple2", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra": "data"}, clear=False, repeat=True)
    failed |= test_call_service_template("test_service_simple3", my_predbat, inv, service_name="test_service", domain="discharge", data={"test": "data"}, extra_data={"extra": "data"}, clear=False)
    failed |= test_call_service_template("test_service_simple4", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra": "data"}, clear=False, repeat=True)
    failed |= test_call_service_template("test_service_simple5", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra": "data2"}, clear=False, repeat=False)

    failed |= test_call_service_template("test_service_simple6", my_predbat, inv, service_name="test_service", domain="charge", data={"test": "data"}, extra_data={"extra_dummy": "data2"}, clear=False, repeat=False)

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

    inv.soc_percent = 49

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


def run_single_debug(test_name, my_predbat, debug_file, expected_file=None):
    print("**** Running debug test {} ****\n".format(debug_file))
    if not expected_file:
        re_do_rates = False
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
    # my_predbat.fetch_config_options()

    # Force off combine export XXX:
    print("Combined export slots {} min_improvement_export {} set_export_freeze_only {}".format(my_predbat.combine_export_slots, my_predbat.metric_min_improvement_export, my_predbat.set_export_freeze_only))
    if not expected_file:
        my_predbat.args["plan_debug"] = True
        my_predbat.set_discharge_during_charge = True
        # my_predbat.metric_self_sufficiency = 0
        # my_predbat.calculate_second_pass = False
        # my_predbat.best_soc_keep = 1
        pass
        # my_predbat.combine_export_slots = False
        # my_predbat.best_soc_keep = 1.0
        # my_predbat.metric_min_improvement_export = 5

    if re_do_rates:
        # Set rate thresholds
        if my_predbat.rate_import or my_predbat.rate_export:
            my_predbat.set_rate_thresholds()

        # Find discharging windows
        if my_predbat.rate_export:
            my_predbat.high_export_rates, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_export, 5, my_predbat.rate_export_cost_threshold, True)
            # Update threshold automatically
            if my_predbat.rate_high_threshold == 0 and lowest <= my_predbat.rate_export_max:
                my_predbat.rate_export_cost_threshold = lowest

        # Find charging windows
        if my_predbat.rate_import:
            # Find charging window
            my_predbat.low_rates, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False)
            # Update threshold automatically
            if my_predbat.rate_low_threshold == 0 and highest >= my_predbat.rate_min:
                my_predbat.rate_import_cost_threshold = highest

    print("minutes_now {} end_record {}".format(my_predbat.minutes_now, my_predbat.end_record))

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

    my_predbat.args["threads"] = 0
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
        assert_charge_rate=2000,
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
        "single",
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
        expect_charge_limit.append(10 if price <= 5.0 else 0)
    expect_charge_limit[26] = 0.5  # freeze
    expect_charge_limit[42] = 0.5  # freeze
    failed |= run_optimise_all_windows(
        "created2",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=40.3,
        inverter_loss=0.9,
        best_soc_keep=0.0,
        battery_size=10,
    )

    if failed:
        return failed

    # One extra charge as we will fall below keep otherwise
    expect_charge_limit[9] = 0.5
    expect_charge_limit[10] = 0.5
    failed |= run_optimise_all_windows(
        "created3",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=29.5,
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
        expect_charge_limit.append(10 if price <= 5.0 else 0)

    expect_charge_limit[6] = 0.5
    expect_charge_limit[22] = 0.5
    expect_charge_limit[38] = 0.5
    failed |= run_optimise_all_windows(
        "created4",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=28.8,
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
    if best_cost != 28.8:
        print("ERROR: Expected best cost to be 28.8 but got {}".format(best_cost))
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
        "off_peak",
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


def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Predbat unit tests")
    parser.add_argument("--debug_file", action="store", help="Enable debug output")
    parser.add_argument("--quick", action="store_true", help="Run quick tests")
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
        run_single_debug(args.debug_file, my_predbat, args.debug_file)
        sys.exit(0)

    free_sessions = my_predbat.download_octopus_free("http://octopus.energy/free-electricity")
    free_sessions = my_predbat.download_octopus_free("http://octopus.energy/free-electricity")
    if not free_sessions:
        print("**** ERROR: No free sessions found ****")
        failed = 1
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
    if not failed:
        failed |= run_nordpool_test(my_predbat)

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
