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
from datetime import datetime, timedelta
from config import PREDICT_STEP, RUN_EVERY, TIME_FORMAT
from utils import remove_intersecting_windows, get_charge_rate_curve, get_discharge_rate_curve, find_charge_rate, calc_percent_limit


# Only assign globals once to avoid re-creating them with processes are forked
if not "PRED_GLOBAL" in globals():
    PRED_GLOBAL = {}


def reset_prediction_globals():
    global PRED_GLOBAL
    PRED_GLOBAL = {}


def wrapped_run_prediction_single(charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, step):
    global PRED_GLOBAL
    pred = Prediction()
    pred.__dict__ = PRED_GLOBAL["dict"].copy()
    return pred.thread_run_prediction_single(charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, step)


def wrapped_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record):
    global PRED_GLOBAL
    pred = Prediction()
    pred.__dict__ = PRED_GLOBAL["dict"].copy()
    return pred.thread_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record)


def wrapped_run_prediction_discharge(this_discharge_limit, start, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record):
    global PRED_GLOBAL
    pred = Prediction()
    pred.__dict__ = PRED_GLOBAL["dict"].copy()
    return pred.thread_run_prediction_discharge(this_discharge_limit, start, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record)

def get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss):
    """
    Get AC output difference
    """
    battery_balance = (battery_draw + pv_dc)
    battery_balance = battery_balance * inverter_loss if battery_balance > 0 else battery_balance / inverter_loss
    diff = load_yesterday - battery_balance - pv_ac
    return diff

def get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid):
    """
    Get total inverter power
    """
    battery_balance = (battery_draw + pv_dc)

    if inverter_hybrid:
        total_inverted = (abs(battery_balance) + pv_ac) / inverter_loss
    else:
        total_inverted = abs(battery_balance) / inverter_loss
    return total_inverted

class Prediction:
    """
    Class to hold prediction input and output data and the run function
    """

    def __init__(self, base=None, pv_forecast_minute_step=None, pv_forecast_minute10_step=None, load_minutes_step=None, load_minutes_step10=None):
        global PRED_GLOBAL
        if base:
            self.minutes_now = base.minutes_now
            self.log = base.log
            self.forecast_minutes = base.forecast_minutes
            self.midnight_utc = base.midnight_utc
            self.soc_kw = base.soc_kw
            self.soc_max = base.soc_max
            self.export_today_now = base.export_today_now
            self.import_today_now = base.import_today_now
            self.load_minutes_now = base.load_minutes_now
            self.pv_today_now = base.pv_today_now
            self.iboost_today = base.iboost_today
            self.charge_rate_now = base.charge_rate_now
            self.discharge_rate_now = base.discharge_rate_now
            self.cost_today_sofar = base.cost_today_sofar
            self.carbon_today_sofar = base.carbon_today_sofar
            self.debug_enable = base.debug_enable
            self.num_cars = base.num_cars
            self.car_charging_soc = base.car_charging_soc
            self.car_charging_soc_next = base.car_charging_soc_next
            self.car_charging_loss = base.car_charging_loss
            self.reserve = base.reserve
            self.metric_standing_charge = base.metric_standing_charge
            self.set_charge_freeze = base.set_charge_freeze
            self.set_reserve_enable = base.set_reserve_enable
            self.set_discharge_freeze = base.set_discharge_freeze
            self.set_discharge_freeze_only = base.set_discharge_freeze_only
            self.set_discharge_during_charge = base.set_discharge_during_charge
            self.set_read_only = base.set_read_only
            self.set_charge_low_power = base.set_charge_low_power
            self.car_charging_slots = base.car_charging_slots
            self.car_charging_limit = base.car_charging_limit
            self.car_charging_from_battery = base.car_charging_from_battery
            self.iboost_enable = base.iboost_enable
            self.carbon_enable = base.carbon_enable
            self.iboost_next = base.iboost_next
            self.iboost_max_energy = base.iboost_max_energy
            self.iboost_gas = base.iboost_gas
            self.iboost_gas_scale = base.iboost_gas_scale
            self.iboost_max_power = base.iboost_max_power
            self.iboost_min_power = base.iboost_min_power
            self.iboost_min_soc = base.iboost_min_soc
            self.iboost_solar = base.iboost_solar
            self.iboost_charging = base.iboost_charging
            self.iboost_running = base.iboost_running
            self.inverter_loss = base.inverter_loss
            self.inverter_hybrid = base.inverter_hybrid
            self.inverter_limit = base.inverter_limit
            self.export_limit = base.export_limit
            self.battery_rate_min = base.battery_rate_min
            self.battery_rate_max_charge = base.battery_rate_max_charge
            self.battery_rate_max_discharge = base.battery_rate_max_discharge
            self.battery_rate_max_charge_scaled = base.battery_rate_max_charge_scaled
            self.battery_rate_max_discharge_scaled = base.battery_rate_max_discharge_scaled
            self.battery_charge_power_curve = base.battery_charge_power_curve
            self.battery_discharge_power_curve = base.battery_discharge_power_curve
            self.battery_rate_max_scaling = base.battery_rate_max_scaling
            self.battery_rate_max_scaling_discharge = base.battery_rate_max_scaling_discharge
            self.battery_loss = base.battery_loss
            self.battery_loss_discharge = base.battery_loss_discharge
            self.best_soc_keep = base.best_soc_keep
            self.best_soc_min = base.best_soc_min
            self.car_charging_battery_size = base.car_charging_battery_size
            self.rate_gas = base.rate_gas
            self.rate_import = base.rate_import
            self.rate_export = base.rate_export
            self.pv_forecast_minute_step = pv_forecast_minute_step
            self.pv_forecast_minute10_step = pv_forecast_minute10_step
            self.load_minutes_step = load_minutes_step
            self.load_minutes_step10 = load_minutes_step10
            self.carbon_intensity = base.carbon_intensity

            # Store this dictionary in global so we can reconstruct it in the thread without passing the data
            PRED_GLOBAL["dict"] = self.__dict__.copy()

    def thread_run_prediction_single(self, charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, step):
        """
        Run single prediction in a thread
        """
        cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record=end_record, step=step
        )
        return (cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g)

    def thread_run_prediction_charge(self, try_soc, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record):
        """
        Run prediction in a thread
        """

        try_charge_limit = charge_limit.copy()
        if all_n:
            for set_n in all_n:
                try_charge_limit[set_n] = try_soc
        else:
            try_charge_limit[window_n] = try_soc

        cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            try_charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record=end_record
        )
        min_soc = 0
        max_soc = self.soc_max
        if not all_n:
            window = charge_window[window_n]
            predict_minute_start = max(int((window["start"] - self.minutes_now) / 5) * 5, 0)
            predict_minute_end = int((window["end"] - self.minutes_now) / 5) * 5
            if (predict_minute_start in self.predict_soc) and (predict_minute_end in self.predict_soc):
                min_soc = min(self.predict_soc[predict_minute_start], self.predict_soc[predict_minute_end])
            if (predict_minute_start in self.predict_soc) and (predict_minute_end in self.predict_soc):
                max_soc = max(self.predict_soc[predict_minute_start], self.predict_soc[predict_minute_end])

        return (
            cost,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            soc,
            soc_min_minute,
            battery_cycle,
            metric_keep,
            final_iboost,
            final_carbon_g,
            min_soc,
            max_soc,
        )

    def thread_run_prediction_discharge(self, this_discharge_limit, start, window_n, charge_limit, charge_window, discharge_window, discharge_limits, pv10, all_n, end_record):
        """
        Run prediction in a thread
        """
        # Store try value into the window
        if all_n:
            for window_id in all_n:
                discharge_limits[window_id] = this_discharge_limit
        else:
            discharge_limits[window_n] = this_discharge_limit
            # Adjust start
            window = discharge_window[window_n]
            start = min(start, window["end"] - 5)
            discharge_window[window_n]["start"] = start

        metricmid, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record=end_record
        )
        return metricmid, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g

    def find_charge_window_optimised(self, charge_windows):
        """
        Takes in an array of charge windows
        Returns a dictionary defining for each minute that is in the charge window will contain the window number
        """
        charge_window_optimised = {}
        for window_n in range(len(charge_windows)):
            for minute in range(charge_windows[window_n]["start"], charge_windows[window_n]["end"], PREDICT_STEP):
                charge_window_optimised[minute] = window_n
        return charge_window_optimised

    def in_car_slot(self, minute):
        """
        Is the given minute inside a car slot
        """
        load_amount = [0 for car_n in range(self.num_cars)]

        for car_n in range(self.num_cars):
            if self.car_charging_slots[car_n]:
                for slot in self.car_charging_slots[car_n]:
                    start_minutes = slot["start"]
                    end_minutes = slot["end"]
                    kwh = slot["kwh"]
                    slot_minutes = end_minutes - start_minutes
                    slot_hours = slot_minutes / 60.0

                    # Return the load in that slot
                    if minute >= start_minutes and minute < end_minutes:
                        load_amount[car_n] = abs(kwh / slot_hours)
                        break
        return load_amount

    
    def run_prediction(self, charge_limit, charge_window, discharge_window, discharge_limits, pv10, end_record, save=None, step=PREDICT_STEP):
        """
        Run a prediction scenario given a charge limit, return the results
        """

        # Fetch data from globals, optimised away from class to avoid passing it between threads
        if pv10:
            pv_forecast_minute_step = self.pv_forecast_minute10_step
            load_minutes_step = self.load_minutes_step10
        else:
            pv_forecast_minute_step = self.pv_forecast_minute_step
            load_minutes_step = self.load_minutes_step
        rate_gas = self.rate_gas
        rate_import = self.rate_import
        rate_export = self.rate_export

        # Data structures creating during the prediction
        self.predict_soc = {}
        self.predict_soc_best = {}
        self.predict_metric_best = {}
        self.predict_iboost_best = {}
        self.predict_carbon_best = {}

        predict_export = {}
        predict_battery_power = {}
        predict_battery_cycle = {}
        predict_soc_time = {}
        predict_car_soc_time = [{} for car_n in range(self.num_cars)]
        predict_pv_power = {}
        predict_state = {}
        predict_grid_power = {}
        predict_load_power = {}
        predict_iboost = {}
        predict_carbon_g = {}
        minute = 0
        minute_left = self.forecast_minutes
        soc = self.soc_kw
        soc_min = self.soc_max
        soc_min_minute = self.minutes_now
        charge_has_run = False
        charge_has_started = False
        discharge_has_run = False
        export_kwh = self.export_today_now
        export_kwh_h0 = export_kwh
        import_kwh = self.import_today_now
        import_kwh_h0 = import_kwh
        load_kwh = self.load_minutes_now
        load_kwh_h0 = load_kwh
        pv_kwh = self.pv_today_now
        pv_kwh_h0 = pv_kwh
        iboost_today_kwh = self.iboost_today
        import_kwh_house = 0
        import_kwh_battery = 0
        carbon_g = self.carbon_today_sofar
        battery_cycle = 0
        metric_keep = 0
        four_hour_rule = True
        final_export_kwh = export_kwh
        final_import_kwh = import_kwh
        final_load_kwh = load_kwh
        final_pv_kwh = pv_kwh
        final_iboost_kwh = iboost_today_kwh
        final_import_kwh_house = import_kwh_house
        final_import_kwh_battery = import_kwh_battery
        final_battery_cycle = battery_cycle
        final_metric_keep = metric_keep
        final_carbon_g = carbon_g
        metric = self.cost_today_sofar
        final_soc = soc
        first_charge_soc = soc
        prev_soc = soc
        final_metric = metric
        metric_time = {}
        load_kwh_time = {}
        pv_kwh_time = {}
        export_kwh_time = {}
        import_kwh_time = {}
        record_time = {}
        car_soc = self.car_charging_soc[:]
        final_car_soc = car_soc[:]
        charge_rate_now = self.charge_rate_now
        discharge_rate_now = self.discharge_rate_now
        battery_state = "-"
        grid_state = "-"
        first_charge = end_record
        export_to_first_charge = 0

        # Remove intersecting windows and optimise the data format of the charge/discharge window
        charge_limit, charge_window = remove_intersecting_windows(charge_limit, charge_window, discharge_limits, discharge_window)
        charge_window_optimised = self.find_charge_window_optimised(charge_window)
        discharge_window_optimised = self.find_charge_window_optimised(discharge_window)

        # For the SOC calculation we need to stop 24 hours after the first charging window starts
        # to avoid wrapping into the next day
        record = True

        # Simulate each forward minute
        while minute < self.forecast_minutes:
            # Minute yesterday can wrap if days_previous is only 1
            minute_absolute = minute + self.minutes_now
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * minute_absolute)
            prev_soc = soc
            reserve_expected = self.reserve

            # Once a force discharge is set the four hour rule is disabled
            if four_hour_rule:
                keep_minute_scaling = min((minute / (4 * 60)), 1.0) * 0.5
            else:
                keep_minute_scaling = 0.5

            # Find charge & discharge windows
            charge_window_n = charge_window_optimised.get(minute_absolute, -1)
            discharge_window_n = discharge_window_optimised.get(minute_absolute, -1)

            # Find charge limit
            charge_limit_n = 0
            if charge_window_n >= 0:
                charge_limit_n = charge_limit[charge_window_n]
                if charge_limit_n == 0:
                    charge_window_n = -1
                else:
                    if self.set_charge_freeze and (charge_limit_n == self.reserve):
                        # Charge freeze via reserve
                        charge_limit_n = soc

                    # When set reserve enable is on pretend the reserve is the charge limit minus the
                    # minimum battery rate modelled as it can leak a little
                    if self.set_reserve_enable:
                        reserve_expected = max(charge_limit_n - self.battery_rate_min * step, self.reserve)

            # Outside the recording window?
            if minute >= end_record and record:
                record = False

            # Store data before the next simulation step to align timestamps
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if self.debug_enable or save:
                predict_soc_time[stamp] = round(soc, 3)
                metric_time[stamp] = round(metric, 3)
                load_kwh_time[stamp] = round(load_kwh, 3)
                pv_kwh_time[stamp] = round(pv_kwh, 2)
                import_kwh_time[stamp] = round(import_kwh, 2)
                export_kwh_time[stamp] = round(export_kwh, 2)
                for car_n in range(self.num_cars):
                    predict_car_soc_time[car_n][stamp] = round(car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0, 2)
                predict_iboost[stamp] = iboost_today_kwh
                record_time[stamp] = 0 if record else self.soc_max

            # Save Soc prediction data as minutes for later use
            self.predict_soc[minute] = round(soc, 3)
            if save and (save in ["best", "test"]):
                self.predict_soc_best[minute] = round(soc, 3)
                self.predict_metric_best[minute] = round(metric, 3)
                self.predict_iboost_best[minute] = iboost_today_kwh
                self.predict_carbon_best[minute] = carbon_g

            # Add in standing charge, only for the final plan when we save the results
            if (minute_absolute % (24 * 60)) < step and (save in ["best", "base", "base10", "best10", "test"]):
                metric += self.metric_standing_charge

            # Get load and pv forecast, total up for all values in the step
            pv_now = 0
            load_yesterday = 0
            for offset in range(0, step, PREDICT_STEP):
                pv_now += pv_forecast_minute_step.get(minute + offset, 0)
                load_yesterday += load_minutes_step.get(minute + offset, 0)

            # Count PV kWh
            pv_kwh += pv_now
            if record:
                final_pv_kwh = pv_kwh

            # Simulate car charging
            car_load = self.in_car_slot(minute_absolute)

            # Car charging?
            car_freeze = False
            for car_n in range(self.num_cars):
                if car_load[car_n] > 0.0:
                    car_load_scale = car_load[car_n] * step / 60.0
                    car_load_scale = car_load_scale * self.car_charging_loss
                    car_load_scale = max(min(car_load_scale, self.car_charging_limit[car_n] - car_soc[car_n]), 0)
                    car_soc[car_n] = round(car_soc[car_n] + car_load_scale, 3)
                    load_yesterday += car_load_scale / self.car_charging_loss
                    # Model not allowing the car to charge from the battery
                    if not self.car_charging_from_battery:
                        discharge_rate_now = self.battery_rate_min  # 0
                        car_freeze = True

            if save == "test" and ((minute % 60) == 0):
                print("Minute {} num_cars {} Car load {} car_freeze {}".format(minute, self.num_cars, car_load, car_freeze))

            # Reset modelled discharge rate if no car is charging
            if not self.car_charging_from_battery and not car_freeze:
                discharge_rate_now = self.battery_rate_max_discharge

            # IBoost solar diverter on load, don't do on discharge
            iboost_amount = 0
            if self.iboost_enable and (discharge_window_n < 0):
                if iboost_today_kwh < self.iboost_max_energy:
                    if self.iboost_gas:
                        if rate_gas:
                            # iBoost on cheap electric rates
                            gas_rate = rate_gas.get(minute_absolute, 99) * self.iboost_gas_scale
                            electric_rate = rate_import.get(minute_absolute, 0)
                            if (electric_rate < gas_rate) and (charge_window_n >= 0 or not self.iboost_charging):
                                iboost_amount = min(self.iboost_max_power * step, self.iboost_max_energy - iboost_today_kwh)
                                load_yesterday += iboost_amount
                    elif self.iboost_charging:
                        if charge_window_n >= 0:
                            iboost_amount = min(self.iboost_max_power * step, self.iboost_max_energy - iboost_today_kwh)
                            load_yesterday += iboost_amount
                
            # Count load
            load_kwh += load_yesterday
            if record:
                final_load_kwh = load_kwh

            # discharge freeze, reset charge rate by default
            if self.set_discharge_freeze:
                charge_rate_now = self.battery_rate_max_charge
                # Freeze mode
                if (discharge_window_n >= 0) and (discharge_limits[discharge_window_n] == 99.0 or self.set_discharge_freeze_only):
                    charge_rate_now = self.battery_rate_min  # 0

            # Set discharge during charge?
            if not self.set_discharge_during_charge:
                if (charge_window_n >= 0) and ((soc >= charge_limit_n) or not self.set_reserve_enable):
                    discharge_rate_now = self.battery_rate_min  # 0
                elif not car_freeze:
                    # Reset discharge rate
                    discharge_rate_now = self.battery_rate_max_discharge

            # Battery behaviour
            if self.inverter_hybrid:
                inverter_loss_ac = self.inverter_loss
            else:
                inverter_loss_ac = 1.0
            inverter_loss = self.inverter_loss

            charge_rate_now_curve = get_charge_rate_curve(self, soc, charge_rate_now)
            discharge_rate_now_curve = get_discharge_rate_curve(self, soc, discharge_rate_now)
            battery_to_min = max(soc - reserve_expected, 0) * self.battery_loss_discharge
            battery_to_max = max(self.soc_max - soc, 0) * self.battery_loss
            inverter_limit = self.inverter_limit * step
            export_limit = self.export_limit * step

            discharge_min = self.reserve
            use_keep = self.best_soc_keep if four_hour_rule else self.reserve
            if discharge_window_n >= 0:
                discharge_min = max(self.soc_max * discharge_limits[discharge_window_n] / 100.0, self.reserve, use_keep, self.best_soc_min)

            if save=="test" and ((minute % 60) == 0):
                print("Minute {} metric {} charge_rate_now_curve {} discharge_rate_now_curve {} soc {} pv_now {} rate_max_charge {} rate_max_scaling {} curve {} get {}".format(minute, metric, charge_rate_now_curve*60, discharge_rate_now_curve*60, soc, pv_now * 60/step, self.battery_rate_max_charge*60, self.battery_rate_max_scaling, self.battery_charge_power_curve, get_charge_rate_curve(self, soc, charge_rate_now, debug=True)*60))

            if (
                not self.set_discharge_freeze_only
                and (discharge_window_n >= 0)
                and discharge_limits[discharge_window_n] < 100.0
                and (soc - step * self.battery_rate_max_discharge_scaled) > discharge_min
            ):
                # Discharge enable
                discharge_rate_now = self.battery_rate_max_discharge_scaled  # Assume discharge becomes enabled here
                discharge_rate_now_curve = get_discharge_rate_curve(self, soc, discharge_rate_now)

                # It's assumed if SOC hits the expected reserve then it's terminated
                reserve_expected = max((self.soc_max * discharge_limits[discharge_window_n]) / 100.0, self.reserve)
                battery_to_min = max(soc - reserve_expected, 0) * self.battery_loss_discharge
                battery_draw = min(discharge_rate_now_curve * step, battery_to_min)

                pv_ac = pv_now * inverter_loss_ac
                pv_dc = 0

                # Exceed export limit?
                diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
                if diff < 0 and abs(diff) > export_limit:
                    over_limit = abs(diff) - export_limit
                    reduce_by = over_limit * inverter_loss
                    if reduce_by > battery_draw:
                        battery_draw = max(battery_draw - reduce_by, -battery_to_min, -charge_rate_now_curve * step)
                    else:
                        battery_draw = min(battery_draw - reduce_by, battery_to_max, discharge_rate_now_curve * step)
                    
                    if self.inverter_hybrid:
                        if battery_draw < 0:
                            pv_dc = min(abs(battery_draw), pv_now)
                        pv_ac = (pv_now  - pv_dc) * inverter_loss_ac

                    if save=="test" and ((minute % 60) == 0):
                        diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
                        print("Minute DischargeE {} battery_draw {} battery_state {} pc_ac {} pv_dc {} load {} diff {} over_export_limit {}".format(minute, battery_draw * 60/step, battery_state, pv_ac * 60/step, pv_dc * 60/step, load_yesterday * 60/step, diff * 60/step, over_limit * 60/step))

                # Exceeds inverter limit, scale back discharge?
                total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, self.inverter_hybrid)
                if self.inverter_hybrid:
                    if total_inverted > inverter_limit:
                        over_limit = total_inverted - inverter_limit
                        reduce_by = over_limit * inverter_loss

                        if reduce_by > battery_draw:
                            battery_draw = max(battery_draw - reduce_by, -battery_to_min, -charge_rate_now_curve * step)
                        else:
                            battery_draw = min(battery_draw - reduce_by, battery_to_max, discharge_rate_now_curve * step)

                        if battery_draw < 0:
                            pv_dc = min(abs(battery_draw), pv_now)
                        pv_ac = (pv_now  - pv_dc) * inverter_loss_ac

                        if save=="test" and ((minute % 60) == 0):
                            diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
                            print("Minute Discharge Hybrid Inv Limit {} battery_draw {} battery_state {} pc_ac {} pv_dc {} load {} diff {} over_limit {}".format(minute, battery_draw * 60/step, battery_state, pv_ac * 60/step, pv_dc * 60/step, load_yesterday * 60/step, diff * 60/step, over_limit * 60/step))
                else:
                    if total_inverted > inverter_limit:
                        over_limit = total_inverted - inverter_limit
                        battery_draw = max(battery_draw - over_limit * inverter_loss, 0)

                        if save=="test" and ((minute % 60) == 0):
                            diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
                            print("Minute Discharge AC Inv Limit {} battery_draw {} battery_state {} pc_ac {} pv_dc {} load {} diff {} over_limit {}".format(minute, battery_draw * 60/step, battery_state, pv_ac * 60/step, pv_dc * 60/step, load_yesterday * 60/step, diff * 60/step, over_limit * 60/step))

                battery_state = "f-"

                if battery_draw < 0:
                    battery_state = "f/"

                if save=="test" and ((minute % 60) == 0):
                    diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
                    print("Minute Discharge2 {} battery_draw {} battery_state {} pc_ac {} pv_dc {} load {} diff {}".format(minute, battery_draw, battery_state, pv_ac, pv_dc, load_yesterday, diff))
                
                # Once force discharge starts the four hour rule is disabled
                four_hour_rule = False
            elif (charge_window_n >= 0) and soc < charge_limit_n:
                # Charge enable
                if save in ["best", "best10", "test"]:
                    # Only tune charge rate on final plan not every simulation
                    charge_rate_now = (
                        find_charge_rate(self, minute_absolute, soc, charge_window[charge_window_n], charge_limit_n, self.battery_rate_max_charge) * self.battery_rate_max_scaling
                    )
                else:
                    charge_rate_now = self.battery_rate_max_charge  # Assume charge becomes enabled here

                # Apply the charging curve
                charge_rate_now_curve = get_charge_rate_curve(self, soc, charge_rate_now)

                battery_draw = -max(min(charge_rate_now_curve * step, charge_limit_n - soc), 0, -battery_to_max)
                battery_state = "f+"
                first_charge = min(first_charge, minute)

                if self.inverter_hybrid:
                    pv_dc = min(abs(battery_draw), pv_now)
                else:
                    pv_dc = 0
                pv_ac = (pv_now - pv_dc) * inverter_loss_ac

                if save=="test" and ((minute % 60) == 0):
                    diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
                    print("Minute Charge {} battery_draw {} battery_state {} pc_ac {} pv_dc {} load {} diff {}".format(minute, battery_draw * 60/step, battery_state, pv_ac * 60/step, pv_dc * 60/step, load_yesterday * 60/step, diff * 60/step))

            else:
                # ECO Mode
                pv_ac = pv_now * inverter_loss_ac
                pv_dc = 0
                diff = get_diff(0, pv_dc, pv_ac, load_yesterday, inverter_loss)

                required_for_load = load_yesterday / inverter_loss_ac
                if self.inverter_hybrid:
                    potential_to_charge = pv_now
                else:
                    potential_to_charge = pv_ac

                diff = required_for_load - potential_to_charge

                if diff > 0:
                    battery_draw = min(diff, discharge_rate_now_curve * step, self.inverter_limit * step, battery_to_min)
                    battery_state = "e-"
                else:
                    battery_draw = max(diff, -charge_rate_now_curve * step, -self.inverter_limit * step, -battery_to_max)
                    if battery_draw < 0:
                        battery_state = "e+"
                    else:
                        battery_state = "e~"

                    if self.inverter_hybrid:
                        pv_dc = min(abs(battery_draw), pv_now)
                    else:
                        pv_dc = 0
                    pv_ac = (pv_now - pv_dc) * inverter_loss_ac

                if save=="test" and ((minute % 60) == 0):
                    diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
                    print("Minute ECO {} battery_draw {} battery_state {} pc_ac {} pv_dc {} load {} diff {}".format(minute, battery_draw * 60/step, battery_state, pv_ac * 60/step, pv_dc * 60/step, load_yesterday * 60/step, diff * 60/step))

            # Clamp at inverter limit
            if self.inverter_hybrid:
                battery_inverted = abs(battery_draw + pv_dc) / inverter_loss
                if battery_inverted > inverter_limit:
                    over_limit = battery_inverted - inverter_limit

                    if battery_draw + pv_dc > 0:
                        battery_draw = max(battery_draw - over_limit * inverter_loss, 0)
                    else:
                        battery_draw = min(battery_draw + over_limit * inverter_loss, 0)
                    
                    # Adjustment to charging from solar case
                    if battery_draw < 0:
                        pv_dc = min(abs(battery_draw), pv_now)
                        pv_ac = (pv_now - pv_dc) * inverter_loss_ac

                    if save=="test" and ((minute % 60) == 0):
                        diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
                        print("Minute Clamp-Hybrid1 {} battery_draw {} over_limit {} battery_inverted {} battery_state {} pc_ac {} pv_dc {} load {} diff {}".format(minute, battery_draw * 60/step, over_limit * 60/step, battery_inverted * 60/step, battery_state, pv_ac * 60/step, pv_dc * 60/step, load_yesterday * 60/step, diff * 60/step))

                # Clip solar
                total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, self.inverter_hybrid)
                if total_inverted > inverter_limit:
                    over_limit = total_inverted - inverter_limit
                    pv_ac = max(pv_ac - over_limit * inverter_loss, 0)

                    if save=="test" and ((minute % 60) == 0):
                        diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
                        print("Minute Clamp-Hybrid2 {} battery_draw {} over_limit {} total_inverted {} battery_state {} pc_ac {} pv_dc {} load {} diff {}".format(minute, battery_draw * 60/step, over_limit * 60/step, total_inverted * 60/step, battery_state, pv_ac * 60/step, pv_dc * 60/step, load_yesterday * 60/step, diff * 60/step))
            else:
                total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, self.inverter_hybrid)
                if total_inverted > inverter_limit:
                    over_limit = total_inverted - inverter_limit
                    if battery_draw > 0:
                        battery_draw = max(battery_draw - over_limit * inverter_loss, 0)
                    else:
                        battery_draw = min(battery_draw + over_limit * inverter_loss, 0)

                    if save=="test" and ((minute % 60) == 0):
                        diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
                        print("Minute Clamp-AC {} battery_draw {} over_limit {} total_inverted {} battery_state {} pc_ac {} pv_dc {} load {} diff {}".format(minute, battery_draw * 60/step, over_limit * 60/step, total_inverted * 60/step, battery_state, pv_ac * 60/step, pv_dc * 60/step, load_yesterday * 60/step, diff * 60/step))

            # Export limit, clip PV output
            diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
            if diff < 0 and abs(diff) > export_limit:
                over_limit = abs(diff) - export_limit
                pv_ac = max(pv_ac - over_limit, 0)
                if save=="test" and ((minute % 60) == 0):
                    diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
                    print("Minute Clamp-Export Limit {} battery_draw {} over_limit {} total_inverted {} battery_state {} pc_ac {} pv_dc {} load {} diff {}".format(minute, battery_draw * 60/step, over_limit * 60/step, total_inverted * 60/step, battery_state, pv_ac * 60/step, pv_dc * 60/step, load_yesterday * 60/step, diff * 60/step))

            # Adjust battery soc
            if battery_draw > 0:
                soc = max(soc - battery_draw / self.battery_loss_discharge, reserve_expected)
            else:
                soc = min(soc - battery_draw * self.battery_loss, self.soc_max)
                
            # iBoost solar diverter model
            if self.iboost_enable:
                if iboost_today_kwh < self.iboost_max_energy and (
                    self.iboost_solar and pv_ac > (self.iboost_min_power * step) and ((soc * 100.0 / self.soc_max) >= self.iboost_min_soc)
                ):
                    iboost_amount = min(iboost_amount + pv_ac, self.iboost_max_power * step, self.iboost_max_energy - iboost_today_kwh)
                    pv_ac -= iboost_amount

                # Cumulative energy
                iboost_today_kwh += iboost_amount

                # Model iboost reset
                if (minute_absolute % (24 * 60)) == ((24 * 60) - step):
                    iboost_today_kwh = 0

                # Save iBoost next prediction
                if minute == 0 and save in ["best", "test"]:
                    scaled_boost = (iboost_amount / step) * RUN_EVERY
                    self.iboost_next = round(self.iboost_today + scaled_boost, 3)
                    if iboost_amount > 0:
                        self.iboost_running = True
                    else:
                        self.iboost_running = False

            if save == "test" and ((minute % 60) == 0):
                print("Minute {} iboost amount {} today {} running {} next {} rate_gas {} max_energy {} max_power {}".format(minute, iboost_amount, iboost_today_kwh, self.iboost_running, self.iboost_next, rate_gas.get(minute_absolute, 99), self.iboost_max_energy, self.iboost_max_power))

            # Rounding on SOC
            soc = round(soc, 6)
                
            # Count battery cycles
            battery_cycle = round(battery_cycle + abs(battery_draw), 4)

            # Work out left over energy after battery adjustment
            diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss)
            diff = round(diff, 6)

            if save=="test" and ((minute % 60) == 0):
                print("Minute4 {} diff {} load_yesterday {} pv_dc {} pv_ac {} battery_draw {}".format(minute, diff, load_yesterday, pv_dc, pv_ac, battery_draw))

            # Metric keep - pretend the battery is empty and you have to import instead of using the battery
            if (soc < self.best_soc_keep) and (soc > self.reserve):
                # Apply keep as a percentage of the time in the future so it gets stronger over an 4 hour period
                # Weight to 50% chance of the scenario
                if battery_draw > 0:
                    metric_keep += rate_import[minute_absolute] * battery_draw * keep_minute_scaling
            elif soc < self.best_soc_keep:
                # It seems odd but the reason to add in metric keep when the battery is empty because otherwise you weight an empty battery quite heavily
                # and end up forcing it all to zero
                keep_diff = get_diff(0, 0, pv_now, load_yesterday, inverter_loss)
                if keep_diff > 0:
                    metric_keep += rate_import[minute_absolute] * keep_diff * keep_minute_scaling

            if diff > 0:
                # Import
                # All imports must go to home (no inverter loss) or to the battery (inverter loss accounted before above)
                import_kwh += diff

                if self.carbon_enable:
                    carbon_g += diff * self.carbon_intensity.get(minute, 0)

                if charge_window_n >= 0:
                    # If the battery is on charge anyhow then imports are at battery charging rate
                    import_kwh_battery += diff
                else:
                    # self.log("importing to minute %s amount %s kW total %s kWh total draw %s" % (minute, energy, import_kwh_house, diff))
                    import_kwh_house += diff

                metric += rate_import[minute_absolute] * diff
                grid_state = "<"
            else:
                # Export
                energy = -diff
                export_kwh += energy
                if self.carbon_enable:
                    carbon_g -= energy * self.carbon_intensity.get(minute, 0)

                if minute_absolute in rate_export:
                    metric -= rate_export[minute_absolute] * energy
                if diff != 0:
                    grid_state = ">"
                else:
                    grid_state = "~"

            # Rounding for next stage
            metric = round(metric, 4)
            import_kwh_battery = round(import_kwh_battery, 6)
            import_kwh_house = round(import_kwh_house, 6)
            export_kwh = round(export_kwh, 6)

            # Store the number of minutes until the battery runs out
            if record and soc <= self.reserve:
                minute_left = min(minute, minute_left)

            # Record final soc & metric
            if record:
                final_soc = soc
                for car_n in range(self.num_cars):
                    final_car_soc[car_n] = round(car_soc[car_n], 3)
                    if minute == 0:
                        # Next car SOC
                        self.car_charging_soc_next[car_n] = round(car_soc[car_n], 3)

                final_metric = metric
                final_import_kwh = import_kwh
                final_import_kwh_battery = import_kwh_battery
                final_import_kwh_house = import_kwh_house
                final_export_kwh = export_kwh
                final_iboost_kwh += iboost_amount
                final_battery_cycle = battery_cycle
                final_metric_keep = metric_keep
                final_carbon_g = carbon_g

                # Store export data
                if diff < 0:
                    predict_export[minute] = energy
                    if minute <= first_charge:
                        export_to_first_charge += energy
                else:
                    predict_export[minute] = 0

                # Soc at next charge start
                if minute <= first_charge:
                    first_charge_soc = prev_soc

            # Have we past the charging or discharging time?
            if charge_window_n >= 0:
                charge_has_started = True
            if charge_has_started and (charge_window_n < 0):
                charge_has_run = True
            if (discharge_window_n >= 0) and discharge_limits[discharge_window_n] < 100.0:
                discharge_has_run = True

            # Record soc min
            if record and (discharge_has_run or charge_has_run or not charge_window):
                if soc < soc_min:
                    soc_min_minute = minute_absolute
                soc_min = min(soc_min, soc)

            # Record state
            if self.debug_enable or save:
                predict_state[stamp] = "g" + grid_state + "b" + battery_state
                predict_battery_power[stamp] = round(battery_draw * (60 / step), 3)
                predict_battery_cycle[stamp] = round(battery_cycle, 3)
                predict_pv_power[stamp] = round((pv_forecast_minute_step[minute] + pv_forecast_minute_step.get(minute + step, 0)) * (30 / step), 3)
                predict_grid_power[stamp] = round(diff * (60 / step), 3)
                predict_load_power[stamp] = round(load_yesterday * (60 / step), 3)
                if self.carbon_enable:
                    predict_carbon_g[stamp] = round(carbon_g, 3)

            if save == "test" and self.debug_enable:
               self.log("Best plan, minute {} soc {} charge_limit_n {} battery_cycle {} metric {} metric_keep {} soc_min {} diff {} import_battery {} import_house {} export {}".format(minute, soc, charge_limit_n, battery_cycle, metric, metric_keep, soc_min, diff, import_kwh_battery, import_kwh_house, export_kwh))
            minute += step

        hours_left = minute_left / 60.0

        self.hours_left = hours_left
        self.final_car_soc = final_car_soc
        self.predict_car_soc_time = predict_car_soc_time
        self.final_soc = final_soc
        self.final_metric = final_metric
        self.final_metric_keep = final_metric_keep
        self.final_import_kwh = final_import_kwh
        self.final_import_kwh_battery = final_import_kwh_battery
        self.final_import_kwh_house = final_import_kwh_house
        self.final_export_kwh = final_export_kwh
        self.final_load_kwh = final_load_kwh
        self.final_pv_kwh = final_pv_kwh
        self.final_iboost_kwh = final_iboost_kwh
        self.final_battery_cycle = final_battery_cycle
        self.final_soc_min = soc_min
        self.final_soc_min_minute = soc_min_minute
        self.export_to_first_charge = export_to_first_charge
        self.predict_soc_time = predict_soc_time
        self.first_charge = first_charge
        self.first_charge_soc = first_charge_soc
        self.predict_state = predict_state
        self.predict_battery_power = predict_battery_power
        self.predict_battery_power = predict_battery_power
        self.predict_pv_power = predict_pv_power
        self.predict_grid_power = predict_grid_power
        self.predict_load_power = predict_load_power
        self.predict_iboost = predict_iboost
        self.predict_carbon_g = predict_carbon_g
        self.predict_export = predict_export
        self.metric_time = metric_time
        self.record_time = record_time
        self.predict_battery_cycle = predict_battery_cycle
        self.predict_battery_power = predict_battery_power
        self.pv_kwh_h0 = pv_kwh_h0
        self.import_kwh_h0 = import_kwh_h0
        self.export_kwh_h0 = export_kwh_h0
        self.load_kwh_h0 = load_kwh_h0
        self.load_kwh_time = load_kwh_time
        self.pv_kwh_time = pv_kwh_time
        self.import_kwh_time = import_kwh_time
        self.export_kwh_time = export_kwh_time

        return (
            round(final_metric, 4),
            round(import_kwh_battery, 4),
            round(import_kwh_house, 4),
            round(export_kwh, 4),
            soc_min,
            round(final_soc, 4),
            soc_min_minute,
            round(final_battery_cycle, 4),
            round(final_metric_keep, 4),
            round(final_iboost_kwh, 4),
            round(final_carbon_g, 4),
        )
