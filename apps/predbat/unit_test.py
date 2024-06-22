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
    for minute in range(my_predbat.forecast_minutes + 24 * 60):
        my_predbat.rate_import[minute] = ir
        my_predbat.rate_export[minute] = xr


def reset_inverter(my_predbat):
    my_predbat.inverter_limit = 1000
    my_predbat.num_inverters = 1
    my_predbat.export_limit = 999999
    my_predbat.inverters = [TestInverter()]
    my_predbat.charge_window = []
    my_predbat.discharge_window = []
    my_predbat.discharge_limits = []
    my_predbat.current_charge_limit = 0.0
    my_predbat.soc_kw = 0.0
    my_predbat.soc_max = 100.0
    my_predbat.reserve = 0.0
    my_predbat.reserve_percent = 0.0
    my_predbat.reserve_current = 0.0
    my_predbat.reserve_current_percent = 0.0
    my_predbat.battery_rate_max_charge = 1000
    my_predbat.battery_rate_max_discharge = 1000
    my_predbat.battery_rate_max_charge_scaled = 1000
    my_predbat.battery_rate_max_discharge_scaled = 1000
    my_predbat.battery_rate_min = 0
    my_predbat.charge_rate_now = 1000
    my_predbat.discharge_rate_now = 1000
    my_predbat.pv_power = 0
    my_predbat.load_power = 0


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


def simple_scenario(name, my_predbat, load_amount, pv_amount, assert_final_metric, assert_final_soc):
    """
    No PV, No Load
    """
    print("Run scenario {}".format(name))
    assert_final_metric = round(assert_final_metric / 100.0, 2)
    assert_final_soc = round(assert_final_soc / 100.0, 2)
    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = pv_amount / (60 / 5)
        load_step[minute] = load_amount / (60 / 5)
    prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    charge_limit_best = []
    charge_window_best = []
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
    ) = prediction.run_prediction(charge_limit_best, charge_window_best, [], [], False, end_record=(my_predbat.forecast_minutes), save="best")
    metric = round(metric / 100.0, 2)
    print("Final metric: {} should be {}".format(metric, assert_final_metric))
    assert metric == assert_final_metric
    print("Final SOC {} should be {}".format(final_soc, assert_final_soc))
    assert final_soc == assert_final_soc
    plot(name, prediction)


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
    reset_inverter(my_predbat)
    import_rate = 10.0
    export_rate = 5.0
    reset_rates(my_predbat, import_rate, export_rate)

    print("**** Testing Predbat ****")
    simple_scenario("zero", my_predbat, 0, 0, 0, 0)
    simple_scenario("load_only", my_predbat, 1, 0, import_rate * 24, 0)
    simple_scenario("load_pv", my_predbat, 1, 1, 0, 0)
    simple_scenario("pv_only", my_predbat, 0, 1, -export_rate * 24, 0)


if __name__ == "__main__":
    main()
