# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timedelta
from prediction import wrapped_run_prediction_single, Prediction
from matplotlib import pyplot as plt
import numpy as np


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

    def get_state(self, entity_id, default=None, attribute=None, refresh=False, raw=False):
        if not entity_id:
            return {}
        elif entity_id in self.dummy_items:
            result = self.dummy_items[entity_id]
            if raw:
                return result
            elif isinstance(result, dict):
                if attribute:
                    result = result.get(attribute, "")
                else:
                    result = result.get("state", default)
            else:
                if attribute:
                    result = default
            # print("Getting state: {} attribute {} => {}".format(entity_id, attribute, result))
            return result
        else:
            # print("Getting state: {} attribute {} => default {} ".format(entity_id, attribute, default))
            if attribute:
                return ""
            else:
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
        if attributes:
            self.dummy_items[entity_id] = attributes.copy()
            self.dummy_items[entity_id]["state"] = state
        else:
            self.dummy_items[entity_id] = state
        # print("Item now: {}".format(self.dummy_items[entity_id]))
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
    my_predbat.rate_scan_export(my_predbat.rate_export, print=False)


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
    my_predbat.rate_scan_export(my_predbat.rate_export, print=False)


def update_rates_import(my_predbat, charge_window_best):
    for window in charge_window_best:
        for minute in range(window["start"], window["end"]):
            my_predbat.rate_import[minute] = window["average"]
    my_predbat.rate_scan(my_predbat.rate_import, print=False)


def update_rates_export(my_predbat, export_window_best):
    for window in export_window_best:
        for minute in range(window["start"], window["end"]):
            my_predbat.rate_export[minute] = window["average"]
    my_predbat.rate_scan_export(my_predbat.rate_export, print=False)


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
    my_predbat.car_charging_limit = [100.0, 100.0, 100.0, 100.0]
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
    charge_limit_best=None,
    inverter_loss=1.0,
    battery_rate_max_charge=1.0,
    charge_car=0,
    car_charging_from_battery=True,
    iboost_solar=False,
    iboost_solar_excess=False,
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
    charge_scaling10=1.0,
    assert_iboost_running=False,
    assert_iboost_running_solar=False,
    assert_iboost_running_full=False,
    car_soc=0,
    car_limit=100,
    set_charge_low_power=False,
    set_charge_window=True,
    battery_temperature=20,
    set_export_freeze_only=False,
    inverter_can_charge_during_export=True,
    prediction_handle=None,
    return_prediction_handle=False,
    ignore_failed=False,
    set_charge_freeze=True,
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
    my_predbat.set_charge_freeze = set_charge_freeze
    my_predbat.set_export_freeze_only = set_export_freeze_only

    my_predbat.iboost_enable = iboost_enable
    my_predbat.iboost_gas = iboost_gas
    my_predbat.iboost_gas_export = iboost_gas_export
    my_predbat.iboost_solar = iboost_solar
    my_predbat.iboost_solar_excess = iboost_solar_excess
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
    my_predbat.inverter_can_charge_during_export = inverter_can_charge_during_export
    my_predbat.charge_scaling10 = charge_scaling10

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

    if prediction_handle:
        prediction = prediction_handle
    else:
        prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step)

    compute_charge_limit = False
    if charge_limit_best is None:
        compute_charge_limit = True
        charge_limit_best = []
    if charge > 0:
        if compute_charge_limit:
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
            predict_soc,
            car_charging_soc_next,
            iboost_next,
            iboost_running,
            iboost_running_solar,
            iboost_running_full,
        ) = prediction.run_prediction(charge_limit_best, charge_window_best, export_window_best, export_limit_best, pv10, end_record=(my_predbat.end_record), save=save)
        prediction.predict_soc = predict_soc
        prediction.car_charging_soc_next = car_charging_soc_next
        prediction.iboost_next = iboost_next
        prediction.iboost_running = iboost_running
        prediction.iboost_running_solar = iboost_running_solar
        prediction.iboost_running_full = iboost_running_full
    metric = round(metric / 100.0, 2)
    final_soc = round(final_soc, 2)
    final_iboost = round(final_iboost, 2)

    failed = False
    if abs(metric - assert_final_metric) >= 0.1:
        if not ignore_failed:
            print("ERROR: Metric {} should be {}".format(metric, assert_final_metric))
        failed = True
    if abs(final_soc - assert_final_soc) >= 0.1:
        if not ignore_failed:
            print("ERROR: Final SOC {} should be {}".format(final_soc, assert_final_soc))
        failed = True
    if abs(final_iboost - assert_final_iboost) >= 0.1:
        if not ignore_failed:
            print("ERROR: Final iBoost {} should be {}".format(final_iboost, assert_final_iboost))
        failed = True
    if abs(final_carbon_g - assert_final_carbon) >= 0.1:
        if not ignore_failed:
            print("ERROR: Final Carbon {} should be {}".format(final_carbon_g, assert_final_carbon))
        failed = True
    if abs(metric_keep - assert_keep) >= 0.5:
        if not ignore_failed:
            print("ERROR: Metric keep {} should be {}".format(metric_keep, assert_keep))
        failed = True
    if assert_iboost_running != prediction.iboost_running:
        if not ignore_failed:
            print("ERROR: iBoost running should be {}".format(assert_iboost_running))
        failed = True
    if assert_iboost_running_solar != prediction.iboost_running_solar:
        if not ignore_failed:
            print("ERROR: iBoost running solar should be {}".format(assert_iboost_running_solar))
        failed = True
    if assert_iboost_running_full != prediction.iboost_running_full:
        if not ignore_failed:
            print("ERROR: iBoost running full should be {}".format(assert_iboost_running_full))
        failed = True

    if failed and not ignore_failed:
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
            predict_soc,
            car_charging_soc_next,
            iboost_next,
            iboost_running,
            iboost_running_solar,
            iboost_running_full,
        ) = prediction.run_prediction(charge_limit_best, charge_window_best, export_window_best, export_limit_best, pv10, end_record=(my_predbat.end_record), save=save)
        prediction.predict_soc = predict_soc
        prediction.car_charging_soc_next = car_charging_soc_next
        prediction.iboost_next = iboost_next
        prediction.iboost_running = iboost_running
        prediction.iboost_running_solar = iboost_running_solar
        prediction.iboost_running_full = iboost_running_full
        print("charge_limit_best: {} charge_window_best: {} export_window_best: {} export_limit_best: {} pv10: {}".format(charge_limit_best, charge_window_best, export_window_best, export_limit_best, pv10))
        plot(name, prediction)

    if return_prediction_handle:
        return failed, prediction
    else:
        return failed
