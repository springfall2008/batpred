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
):
    """
    No PV, No Load
    """
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

    assert_final_metric = round(assert_final_metric / 100.0, 2)
    assert_final_soc = round(assert_final_soc, 2)
    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = pv_amount / (60 / 5)
        load_step[minute] = load_amount / (60 / 5)
    prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    charge_limit_best = []
    charge_window_best = []
    if charge > 0:
        charge_limit_best = [charge]
        charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.forecast_minutes + my_predbat.minutes_now, "average": 0}]
    discharge_limit_best = []
    discharge_window_best = []
    if discharge < 100:
        discharge_limit_best = [discharge]
        discharge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.forecast_minutes + my_predbat.minutes_now, "average": 0}]
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
    ) = prediction.run_prediction(charge_limit_best, charge_window_best, discharge_window_best, discharge_limit_best, False, end_record=(my_predbat.forecast_minutes), save="best")
    metric = round(metric / 100.0, 2)
    final_soc = round(final_soc, 2)

    failed = False
    if metric != assert_final_metric:
        print("ERROR: Metric {} should be {}".format(metric, assert_final_metric))
        failed = True
    if abs(final_soc - assert_final_soc) >= 0.1:
        print("ERROR: Final SOC {} should be {}".format(final_soc, assert_final_soc))
        failed = True

    if failed:
        plot(name, prediction)
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
    reset_inverter(my_predbat)
    import_rate = 10.0
    export_rate = 5.0
    reset_rates(my_predbat, import_rate, export_rate)

    print("**** Testing Predbat ****")
    failed = False
    failed |= simple_scenario("zero", my_predbat, 0, 0, 0, 0, with_battery=False)
    failed |= simple_scenario("load_only", my_predbat, 1, 0, assert_final_metric=import_rate * 24, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("load_discharge", my_predbat, 1, 0, assert_final_metric=import_rate * 14, assert_final_soc=0, battery_soc=10.0, with_battery=True)
    failed |= simple_scenario("load_discharge_fast", my_predbat, 2, 0, assert_final_metric=import_rate * 38, assert_final_soc=0, battery_soc=10.0, with_battery=True)
    failed |= simple_scenario("load_discharge_fast_big", my_predbat, 2, 0, assert_final_metric=import_rate * 24, assert_final_soc=76, battery_soc=100.0, with_battery=True)
    failed |= simple_scenario(
        "load_discharge_reserve", my_predbat, 1, 0, assert_final_metric=import_rate * 15, assert_final_soc=1, battery_soc=10.0, with_battery=True, reserve=1.0
    )
    failed |= simple_scenario(
        "load_discharge_loss", my_predbat, 1, 0, assert_final_metric=import_rate * 19, assert_final_soc=0, battery_soc=10.0, with_battery=True, battery_loss=0.5
    )
    failed |= simple_scenario("load_pv", my_predbat, 1, 1, assert_final_metric=0, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("pv_only", my_predbat, 0, 1, assert_final_metric=-export_rate * 24, assert_final_soc=0, with_battery=False)
    failed |= simple_scenario("pv_only_bat", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_loss", my_predbat, 0, 1, assert_final_metric=0, assert_final_soc=12, with_battery=True, battery_loss=0.5)
    failed |= simple_scenario("pv_only_bat_100%", my_predbat, 0, 1, assert_final_metric=-export_rate * 14, assert_final_soc=10, with_battery=True, battery_size=10)
    failed |= simple_scenario("pv_only_bat_ac_clips2", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario("pv_only_bat_ac_clips3", my_predbat, 0, 3, assert_final_metric=-export_rate * 48, assert_final_soc=24, with_battery=True)
    failed |= simple_scenario(
        "pv_only_bat_ac_export_limit", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, export_limit=0.5
    )
    failed |= simple_scenario(
        "pv_only_bat_ac_export_limit_load", my_predbat, 0.5, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, export_limit=0.5
    )
    failed |= simple_scenario("pv_only_bat_dc_clips2", my_predbat, 0, 2, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, hybrid=True)
    failed |= simple_scenario("pv_only_bat_dc_clips3", my_predbat, 0, 3, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, hybrid=True)
    failed |= simple_scenario(
        "pv_only_bat_dc_export_limit", my_predbat, 0, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, export_limit=0.5
    )
    failed |= simple_scenario(
        "pv_only_bat_dc_export_limit_load", my_predbat, 0.5, 3, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=24, with_battery=True, hybrid=True, export_limit=0.5
    )
    failed |= simple_scenario("battery_charge", my_predbat, 0, 0, assert_final_metric=import_rate * 10, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario("battery_charge_load", my_predbat, 1, 0, assert_final_metric=import_rate * 34, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario("battery_charge_pv", my_predbat, 0, 1, assert_final_metric=-export_rate * 14, assert_final_soc=10, with_battery=True, charge=10, battery_size=10)
    failed |= simple_scenario(
        "battery_charge_pv_load",
        my_predbat,
        0.5,
        1,
        assert_final_metric=import_rate * 0.5 * 10 - export_rate * 14 * 0.5,
        assert_final_soc=10,
        with_battery=True,
        charge=10,
        battery_size=10,
    )
    failed |= simple_scenario("battery_discharge", my_predbat, 0, 0, assert_final_metric=-export_rate * 10, assert_final_soc=0, with_battery=True, discharge=0, battery_soc=10)
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
        "battery_discharge_pv4_hybrid", my_predbat, 0, 3, assert_final_metric=-export_rate * 24, assert_final_soc=24, with_battery=True, discharge=0, battery_soc=0, hybrid=True
    )
    failed |= simple_scenario(
        "battery_discharge_frz", my_predbat, 0, 0.5, assert_final_metric=-export_rate * 24 * 0.5, assert_final_soc=10, with_battery=True, discharge=99, battery_soc=10
    )
    failed |= simple_scenario("battery_discharge_hld", my_predbat, 0, 0.5, assert_final_metric=-0, assert_final_soc=10 + 24 * 0.5, with_battery=True, discharge=98, battery_soc=10)

    if failed:
        print("**** ERROR: Some tests failed ****")
        sys.exit(1)
    print("**** Tests passed ****")
    sys.exit(0)


if __name__ == "__main__":
    main()
