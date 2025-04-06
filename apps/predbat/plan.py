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
import traceback
from datetime import datetime, timedelta
from multiprocessing import Pool, cpu_count
from config import PREDICT_STEP, TIME_FORMAT
from utils import calc_percent_limit, dp0, dp1, dp2, dp3, dp4, remove_intersecting_windows
from prediction import Prediction, wrapped_run_prediction_single, wrapped_run_prediction_charge, wrapped_run_prediction_export
import sys

"""
Used to mimic threads when they are disabled
"""


class DummyThread:
    def __init__(self, result):
        """
        Store the data into the class
        """
        self.result = result

    def get(self):
        """
        Return the result
        """
        return self.result


class Plan:
    def optimise_charge_limit_price_threads(
        self,
        price_set,
        price_links,
        window_index,
        record_charge_windows,
        record_export_windows,
        try_charge_limit,
        charge_window,
        export_window,
        export_limits,
        end_record=None,
        region_start=None,
        region_end=None,
        fast=False,
        quiet=False,
        best_metric=9999999,
        best_cost=0,
        best_keep=0,
        best_soc_min=None,
        best_price_charge=None,
        best_price_export=None,
        best_cycle=0,
        best_carbon=0,
        best_import=0,
        best_battery_value=0,
        tried_list=None,
        test_mode=False,
    ):
        """
        Pick an import price threshold which gives the best results
        """
        loop_price = price_set[-1]
        best_price = loop_price
        try_export = export_limits.copy()
        best_limits = try_charge_limit.copy()
        best_export = try_export.copy()
        if best_soc_min is None:
            best_soc_min = self.reserve
        if best_price_charge is None:
            best_price_charge = price_set[-1]
        if best_price_export is None:
            best_price_export = price_set[0]
        step = PREDICT_STEP
        if fast:
            step = 30
        if tried_list is None:
            tried_list = {}

        if region_start:
            region_txt = "Region {} - {}".format(self.time_abs_str(region_start), self.time_abs_str(region_end))
        else:
            region_txt = "All regions"

        # Do we loop on export?
        if self.calculate_best_export and self.calculate_export_first:
            export_enable = True
        else:
            export_enable = False

        # Most expensive first
        all_prices = price_set[::] + [dp1(price_set[-1] - 1)]
        if not quiet:
            self.log("All prices {}".format(all_prices))
            if region_start:
                self.log("Region {} - {}".format(self.time_abs_str(region_start), self.time_abs_str(region_end)))
        window_prices = {}
        window_prices_export = {}

        for loop_price in all_prices:
            pred_table = []
            if self.set_export_freeze and self.set_export_freeze_only:
                export_options = [99]
            elif self.set_export_freeze:
                export_options = [99, 0]
            else:
                export_options = [0]
            for export_option in export_options:
                for modulo in [2, 3, 4, 6, 8, 16, 32]:
                    for divide in [96, 48, 32, 16, 8, 4, 3, 2, 1]:
                        all_n = []
                        all_d = []
                        divide_count_d = 0
                        divide_count_c = 0
                        highest_price_charge = price_set[-1]
                        lowest_price_export = price_set[0]
                        for price in price_set:
                            links = price_links[price]
                            if loop_price >= price:
                                for key in links:
                                    window_n = window_index[key]["id"]
                                    typ = window_index[key]["type"]
                                    if typ == "c":
                                        if region_start and (charge_window[window_n]["start"] > region_end or charge_window[window_n]["end"] < region_start):
                                            pass
                                        else:
                                            window_prices[window_n] = price
                                            if loop_price == price:
                                                if (int(divide_count_c / divide) % modulo) == 0:
                                                    all_n.append(window_n)
                                                divide_count_c += 1
                                            else:
                                                all_n.append(window_n)
                            elif export_enable:
                                # For prices above threshold try export
                                for key in links:
                                    typ = window_index[key]["type"]
                                    window_n = window_index[key]["id"]
                                    if typ == "d":
                                        window_prices_export[window_n] = price
                                        if region_start and (export_window[window_n]["start"] > region_end or export_window[window_n]["end"] < region_start):
                                            pass
                                        else:
                                            if (int(divide_count_d / divide) % modulo) == 0:
                                                all_d.append(window_n)
                                            divide_count_d += 1

                        # Sort for print out
                        all_n.sort()
                        all_d.sort()

                        # This price band setting for charge
                        try_charge_limit = best_limits.copy()
                        for window_n in range(record_charge_windows):
                            if window_n >= len(try_charge_limit):
                                continue

                            if region_start and (charge_window[window_n]["start"] > region_end or charge_window[window_n]["end"] < region_start):
                                continue

                            if charge_window[window_n]["start"] in self.manual_all_times:
                                continue

                            if window_n in all_n:
                                if window_prices[window_n] > highest_price_charge:
                                    highest_price_charge = window_prices[window_n]
                                try_charge_limit[window_n] = self.soc_max
                            else:
                                try_charge_limit[window_n] = 0

                        # Try export on/off
                        try_export = best_export.copy()
                        for window_n in range(record_export_windows):
                            if window_n >= len(export_limits):
                                continue

                            if region_start and (export_window[window_n]["start"] > region_end or export_window[window_n]["end"] < region_start):
                                continue

                            if export_window[window_n]["start"] in self.manual_all_times:
                                continue

                            try_export[window_n] = 100
                            if window_n in all_d:
                                if not self.calculate_export_oncharge:
                                    hit_charge = self.hit_charge_window(self.charge_window_best, export_window[window_n]["start"], export_window[window_n]["end"])
                                    if hit_charge >= 0 and try_charge_limit[hit_charge] > 0.0:
                                        continue
                                if not self.car_charging_from_battery and self.hit_car_window(export_window[window_n]["start"], export_window[window_n]["end"]):
                                    continue
                                if not self.iboost_on_export and self.iboost_enable and self.iboost_plan and (self.hit_charge_window(self.iboost_plan, export_window[window_n]["start"], export_window[window_n]["end"]) >= 0):
                                    continue

                                if window_prices_export[window_n] < lowest_price_export:
                                    lowest_price_export = window_prices_export[window_n]
                                try_export[window_n] = export_option

                        # Skip this one as it's the same as selected already
                        try_hash = str(try_charge_limit) + "_d_" + str(try_export)
                        if try_hash in tried_list:
                            if self.debug_enable and 0:
                                self.log("Skip this optimisation with divide {} windows {} export windows {} export_enable {} as it's the same as previous ones hash {}".format(divide, all_n, all_d, export_enable, try_hash))
                            continue

                        if self.debug_enable and 0:
                            self.log("Try this optimisation with divide {} windows {} export windows {} export_enable {}".format(divide, all_n, all_d, export_enable))

                        tried_list[try_hash] = True

                        pred_item = {}
                        pred_item["handle"] = self.launch_run_prediction_single(try_charge_limit, charge_window, export_window, try_export, False, end_record=end_record, step=step)
                        pred_item["handle10"] = self.launch_run_prediction_single(try_charge_limit, charge_window, export_window, try_export, True, end_record=end_record, step=step)
                        pred_item["charge_limit"] = try_charge_limit.copy()
                        pred_item["export_limit"] = try_export.copy()
                        pred_item["highest_price_charge"] = highest_price_charge
                        pred_item["lowest_price_export"] = lowest_price_export
                        pred_item["loop_price"] = loop_price
                        pred_table.append(pred_item)

            for pred in pred_table:
                handle = pred["handle"]
                handle10 = pred["handle10"]
                try_charge_limit = pred["charge_limit"]
                try_export = pred["export_limit"]
                highest_price_charge = pred["highest_price_charge"]
                lowest_price_export = pred["lowest_price_export"]
                loop_price = pred["loop_price"]

                cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = handle.get()
                # Are we doing 10%?
                if handle10:
                    cost10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10, battery_cycle10, metric_keep10, final_iboost10, final_carbon_g10 = handle10.get()
                else:
                    cost10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10, battery_cycle10, metric_keep10, final_iboost10, final_carbon_g10 = handle.get()

                metric, battery_value = self.compute_metric(end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh)

                # Optimise
                if self.debug_enable:
                    if export_enable:
                        self.log(
                            "Optimise all for buy/sell price band <= {} divide {} modulo {} metric {} keep {} soc_min {} import {} export {} soc {} windows {} export on {}".format(
                                loop_price,
                                divide,
                                modulo,
                                dp4(metric),
                                dp4(metric_keep),
                                dp4(soc_min),
                                dp4(import_kwh_battery + import_kwh_house),
                                dp4(export_kwh),
                                dp4(soc),
                                try_charge_limit,
                                try_export,
                            )
                        )
                    else:
                        self.log(
                            "Optimise all for buy/sell price band <= {} divide {} modulo {} metric {} keep {} soc_min {} import {} export {}  soc {} windows {} export off".format(
                                loop_price,
                                divide,
                                modulo,
                                dp4(metric),
                                dp4(metric_keep),
                                dp4(soc_min),
                                dp4(import_kwh_battery + import_kwh_house),
                                dp4(export_kwh),
                                dp4(soc),
                                try_charge_limit,
                            )
                        )

                # For the first pass just pick the most cost effective threshold, consider soc keep later
                if metric < best_metric:
                    best_metric = metric
                    best_keep = metric_keep
                    best_price = loop_price
                    best_price_charge = highest_price_charge
                    best_price_export = lowest_price_export
                    best_limits = try_charge_limit.copy()
                    best_export = try_export.copy()
                    best_cycle = battery_cycle
                    best_carbon = final_carbon_g
                    best_soc_min = soc_min
                    best_cost = cost
                    best_import = import_kwh_battery + import_kwh_house
                    best_battery_value = battery_value
                    if 1 or not quiet:
                        self.log(
                            "Optimise all charge found best buy/sell price band {} best price threshold {} at cost {} metric {} keep {} cycle {} carbon {} import {} cost {} battery_value {} limits {} export {}".format(
                                loop_price,
                                best_price_charge,
                                dp4(best_cost),
                                dp4(best_metric),
                                dp4(best_keep),
                                dp4(best_cycle),
                                dp0(best_carbon),
                                dp2(best_import),
                                dp4(best_cost),
                                battery_value,
                                best_limits,
                                best_export,
                            )
                        )

        if self.debug_enable:
            self.log(
                "Optimise all charge {} best price threshold {} total simulations {} charges at {} at cost {} metric {} keep {} cycle {} carbon {} import {} cost {} battery_value {} soc_min {} limits {} export {}".format(
                    region_txt,
                    dp4(best_price),
                    len(tried_list),
                    dp4(best_price_charge),
                    dp4(best_cost),
                    dp4(best_metric),
                    dp4(best_keep),
                    dp4(best_cycle),
                    dp0(best_carbon),
                    dp2(best_import),
                    dp4(best_cost),
                    dp4(best_battery_value),
                    dp4(best_soc_min),
                    best_limits,
                    best_export,
                )
            )
        if test_mode:
            # Simulate with medium PV
            (
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
            ) = self.run_prediction(best_limits, charge_window, export_window, best_export, False, end_record=end_record, step=PREDICT_STEP, save="test")
        return (
            best_limits,
            best_export,
            best_price_charge,
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
        )

    def launch_run_prediction_single(self, charge_limit, charge_window, export_window, export_limits, pv10, end_record, step=PREDICT_STEP):
        """
        Launch a thread to run a prediction
        """
        charge_limit = copy.deepcopy(charge_limit)
        export_limits = copy.deepcopy(export_limits)
        if self.pool and self.pool._state == "RUN":
            han = self.pool.apply_async(wrapped_run_prediction_single, (charge_limit, charge_window, export_window, export_limits, pv10, end_record, step))
        else:
            han = DummyThread(self.prediction.thread_run_prediction_single(charge_limit, charge_window, export_window, export_limits, pv10, end_record, step))
        return han

    def launch_run_prediction_charge(self, loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv10, all_n, end_record):
        """
        Launch a thread to run a prediction
        """
        if self.pool and self.pool._state == "RUN":
            han = self.pool.apply_async(wrapped_run_prediction_charge, (loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv10, all_n, end_record))
        else:
            han = DummyThread(self.prediction.thread_run_prediction_charge(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv10, all_n, end_record))
        return han

    def launch_run_prediction_export(self, this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, pv10, all_n, end_record):
        """
        Launch a thread to run a prediction
        """
        if self.pool and self.pool._state == "RUN":
            han = self.pool.apply_async(
                wrapped_run_prediction_export,
                (this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, pv10, all_n, end_record),
            )
        else:
            han = DummyThread(self.prediction.thread_run_prediction_export(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, pv10, all_n, end_record))
        return han

    def scenario_summary_title(self, record_time):
        txt = ""
        minute_start = self.minutes_now - self.minutes_now % 30
        for minute_absolute in range(minute_start, self.forecast_minutes + minute_start, 30):
            this_minute_absolute = max(minute_absolute, self.minutes_now)
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * this_minute_absolute)
            dstamp = minute_timestamp.strftime(TIME_FORMAT)
            stamp = minute_timestamp.strftime("%H:%M")
            if txt:
                txt += ", "
            txt += "%8s" % str(stamp)
            if record_time[dstamp] > 0:
                break
        return txt

    def scenario_summary(self, record_time, datap):
        txt = ""
        minute_start = self.minutes_now - self.minutes_now % 30
        for minute_absolute in range(minute_start, self.forecast_minutes + minute_start, 30):
            this_minute_absolute = max(minute_absolute, self.minutes_now)
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * this_minute_absolute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            value = datap[stamp]
            if not isinstance(value, str):
                value = dp2(value)
                if value > 10000:
                    value = dp0(value)
            if txt:
                txt += ", "
            txt += "%8s" % str(value)
            if record_time[stamp] > 0:
                break
        return txt

    def scenario_summary_state(self, record_time):
        txt = ""
        minute_start = self.minutes_now - self.minutes_now % 30
        for minute_absolute in range(minute_start, self.forecast_minutes + minute_start, 30):
            minute_relative_start = max(minute_absolute - self.minutes_now, 0)
            minute_relative_end = minute_relative_start + 30
            this_minute_absolute = max(minute_absolute, self.minutes_now)
            minute_timestamp = self.midnight_utc + timedelta(seconds=60 * this_minute_absolute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            value = ""

            charge_window_n = -1
            for try_minute in range(this_minute_absolute, minute_absolute + 30, 5):
                charge_window_n = self.in_charge_window(self.charge_window_best, try_minute)
                if charge_window_n >= 0 and self.charge_limit_best[charge_window_n] == 0:
                    charge_window_n = -1
                if charge_window_n >= 0:
                    break

            export_window_n = -1
            for try_minute in range(this_minute_absolute, minute_absolute + 30, 5):
                export_window_n = self.in_charge_window(self.export_window_best, try_minute)
                if export_window_n >= 0 and self.export_limits_best[export_window_n] == 100:
                    export_window_n = -1
                if export_window_n >= 0:
                    break

            soc_percent = calc_percent_limit(self.predict_soc_best.get(minute_relative_start, 0.0), self.soc_max)
            soc_percent_end = calc_percent_limit(self.predict_soc_best.get(minute_relative_end, 0.0), self.soc_max)
            soc_percent_max = max(soc_percent, soc_percent_end)
            soc_percent_min = min(soc_percent, soc_percent_end)

            if charge_window_n >= 0 and export_window_n >= 0:
                value = "Chrg/Exp"
            elif charge_window_n >= 0:
                charge_target = self.charge_limit_best[charge_window_n]
                if charge_target == self.reserve:
                    value = "FrzChrg"
                else:
                    value = "Chrg"
            elif export_window_n >= 0:
                export_target = self.export_limits_best[export_window_n]
                if export_target >= soc_percent_max:
                    if export_target == 99:
                        value = "FrzExp"
                    else:
                        value = "HldExp"
                else:
                    value = "Exp"

            if record_time[stamp] > 0:
                break
            if txt:
                txt += ", "
            txt += "%8s" % str(value)
        return txt

    def record_length(self, charge_window, charge_limit, best_price):
        """
        Limit the forecast length to either the total forecast duration or the start of the last window that falls outside the forecast
        """
        next_charge_start = self.forecast_minutes + self.minutes_now
        if charge_window:
            for window_n in range(len(charge_window)):
                if charge_limit[window_n] > 0 and charge_window[window_n]["average"] <= best_price:
                    next_charge_start = charge_window[window_n]["start"]
                    if next_charge_start < self.minutes_now:
                        next_charge_start = charge_window[window_n]["end"]
                    break

        end_record = min(self.forecast_plan_hours * 60 + next_charge_start, self.forecast_minutes + self.minutes_now)
        max_windows = self.max_charge_windows(end_record, charge_window)
        if len(charge_window) > max_windows:
            end_record = min(end_record, charge_window[max_windows]["start"])
            # If we are within this window then push to the end of it
            if end_record < self.minutes_now:
                end_record = charge_window[max_windows]["end"]

        self.log("Calculated end_record as {} based on best_price {} next_charge_start {} max_windows {}".format(self.time_abs_str(end_record), best_price, self.time_abs_str(next_charge_start), max_windows))
        return end_record - self.minutes_now

    def max_charge_windows(self, end_record_abs, charge_window):
        """
        Work out how many charge windows the time period covers
        """
        charge_windows = 0
        window_n = 0
        for window in charge_window:
            if end_record_abs >= window["end"]:
                charge_windows = window_n + 1
            window_n += 1
        return charge_windows

    def hit_charge_window(self, charge_window, start, end):
        """
        Determines if the given start and end time falls within any of the charge windows.

        Parameters:
        charge_window (list): A list of dictionaries representing charge windows, each containing "start" and "end" keys.
        start (int): The start time of the interval to check.
        end (int): The end time of the interval to check.

        Returns:
        int: The index of the charge window that the interval falls within, or -1 if it doesn't fall within any charge window.
        """
        window_n = 0
        for window in charge_window:
            if end > window["start"] and start < window["end"]:
                return window_n
            window_n += 1
        return -1

    def in_charge_window(self, charge_window, minute_abs):
        """
        Work out if this minute is within the a charge window

        Parameters:
        charge_window (list): A sorted list of dictionaries representing charge windows.
                                Each dictionary should have "start" and "end" keys
                                representing the start and end minutes of the window.

        minute_abs (int): The absolute minute value to check.

        Returns:
        int: The index of the charge window if the minute is within a window,
                otherwise -1.
        """
        window_n = 0
        for window in charge_window:
            if minute_abs >= window["start"] and minute_abs < window["end"]:
                return window_n
            elif window["start"] > minute_abs:
                # As windows are sorted, we can stop searching once we've passed the minute
                break
            window_n += 1
        return -1

    def calculate_plan(self, recompute=True, debug_mode=False, publish=True):
        """
        Calculate the new plan (best)

        sets:
           self.charge_window_best
           self.charge_limit_best
           self.charge_limit_percent_best
           self.export_window_best
           self.export_limits_best
        """

        # Re-compute plan due to time wrap
        if self.plan_last_updated_minutes > self.minutes_now:
            self.log("Force recompute due to start of day")
            recompute = True
            self.plan_valid = False

        # Shift onto next charge window if required
        while self.charge_window_best:
            window = self.charge_window_best[0]
            if window["end"] <= self.minutes_now:
                del self.charge_window_best[0]
                del self.charge_limit_best[0]
                del self.charge_limit_percent_best[0]
                self.log("Current charge window has expired, removing it")
            else:
                break

        # Shift onto next export window if required
        while self.export_window_best:
            window = self.export_window_best[0]
            if window["end"] <= self.minutes_now:
                del self.export_window_best[0]
                del self.export_limits_best[0]
                self.log("Current export window has expired, removing it")
            else:
                break

        # Recompute?
        if recompute:
            # Obtain previous plan data for comparison
            if self.plan_valid:
                charge_limit_best_prev = copy.deepcopy(self.charge_limit_best)
                charge_window_best_prev = copy.deepcopy(self.charge_window_best)
                charge_limit_percent_best_prev = copy.deepcopy(self.charge_limit_percent_best)
                export_window_best_prev = copy.deepcopy(self.export_window_best)
                export_limits_best_prev = copy.deepcopy(self.export_limits_best)
                self.log("Recompute is saving previous plan...")
            else:
                charge_limit_best_prev = None
                charge_window_best_prev = None
                charge_limit_percent_best_prev = None
                export_window_best_prev = None
                export_limits_best_prev = None
                self.log("Recompute, previous plan is invalid...")

            self.plan_valid = False  # In case of crash, plan is now invalid

            # Calculate best charge windows
            if self.low_rates and self.calculate_best_charge and self.set_charge_window:
                # If we are using calculated windows directly then save them
                self.charge_window_best = copy.deepcopy(self.low_rates)
            else:
                # Default best charge window as this one
                self.charge_window_best = copy.deepcopy(self.charge_window)

            # Calculate best export windows
            if self.calculate_best_export and self.set_export_window:
                self.export_window_best = copy.deepcopy(self.high_export_rates)
            else:
                self.export_window_best = copy.deepcopy(self.export_window)

            # Pre-fill best charge limit with the current charge limit
            self.charge_limit_best = [self.current_charge_limit * self.soc_max / 100.0 for i in range(len(self.charge_window_best))]
            self.charge_limit_percent_best = [self.current_charge_limit for i in range(len(self.charge_window_best))]

            # Pre-fill best export enable with Off
            self.export_limits_best = [100.0 for i in range(len(self.export_window_best))]

            self.end_record = self.forecast_minutes
        # Show best windows
        self.log("Best charge window {}".format(self.window_as_text(self.charge_window_best, self.charge_limit_percent_best)))
        self.log("Best export window {}".format(self.window_as_text(self.export_window_best, self.export_limits_best)))

        # Created optimised step data
        self.metric_cloud_coverage = self.get_cloud_factor(self.minutes_now, self.pv_forecast_minute, self.pv_forecast_minute10)
        self.metric_load_divergence = self.get_load_divergence(self.minutes_now, self.load_minutes)
        load_minutes_step = self.step_data_history(
            self.load_minutes,
            self.minutes_now,
            forward=False,
            scale_today=self.load_inday_adjustment,
            scale_fixed=1.0,
            type_load=True,
            load_forecast=self.load_forecast,
            load_scaling_dynamic=self.load_scaling_dynamic,
            cloud_factor=self.metric_load_divergence,
        )
        load_minutes_step10 = self.step_data_history(
            self.load_minutes,
            self.minutes_now,
            forward=False,
            scale_today=self.load_inday_adjustment,
            scale_fixed=self.load_scaling10,
            type_load=True,
            load_forecast=self.load_forecast,
            load_scaling_dynamic=self.load_scaling_dynamic,
            cloud_factor=min(self.metric_load_divergence + 0.5, 1.0) if self.metric_load_divergence else None,
        )
        pv_forecast_minute_step = self.step_data_history(self.pv_forecast_minute, self.minutes_now, forward=True, cloud_factor=self.metric_cloud_coverage)
        pv_forecast_minute10_step = self.step_data_history(self.pv_forecast_minute10, self.minutes_now, forward=True, cloud_factor=min(self.metric_cloud_coverage + 0.2, 1.0) if self.metric_cloud_coverage else None)

        # Save step data for debug
        self.load_minutes_step = load_minutes_step
        self.load_minutes_step10 = load_minutes_step10
        self.pv_forecast_minute_step = pv_forecast_minute_step
        self.pv_forecast_minute10_step = pv_forecast_minute10_step

        # Yesterday data
        if recompute and self.calculate_savings and publish:
            self.calculate_yesterday()

        # Creation prediction object
        self.prediction = Prediction(self, pv_forecast_minute_step, pv_forecast_minute10_step, load_minutes_step, load_minutes_step10)

        # Create pool
        if not self.pool:
            threads = self.get_arg("threads", "auto")
            if threads == "auto":
                self.log("Creating pool of {} processes to match your CPU count".format(cpu_count()))
                self.pool = Pool(processes=cpu_count())
            elif threads:
                self.log("Creating pool of {} processes as per apps.yaml".format(int(threads)))
                self.pool = Pool(processes=int(threads))
            else:
                self.log("Not using threading as threads is set to 0 in apps.yaml")

        # Simulate current settings to get initial data
        metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            self.charge_limit, self.charge_window, self.export_window, self.export_limits, False, end_record=self.end_record
        )

        # Try different battery SoC's to get the best result
        if recompute:
            self.rate_best_cost_threshold_charge = None
            self.rate_best_cost_threshold_export = None

        if self.calculate_best and recompute:
            # Recomputing the plan
            self.log_option_best()

            # Full plan
            self.optimise_all_windows(metric, metric_keep, debug_mode)

            # Tweak plan
            if self.calculate_tweak_plan:
                self.tweak_plan(self.end_record, metric, metric_keep)

            # Update target values, will be refined via clipping
            self.update_target_values()

            # Remove charge windows that overlap with export windows
            self.charge_limit_best, self.charge_window_best = remove_intersecting_windows(self.charge_limit_best, self.charge_window_best, self.export_limits_best, self.export_window_best)

            # Filter out any unused export windows
            if self.calculate_best_export and self.export_window_best:
                # Filter out the windows we disabled
                self.export_limits_best, self.export_window_best = self.discard_unused_export_slots(self.export_limits_best, self.export_window_best)

                # Clipping windows
                if self.export_window_best:
                    # Re-run prediction to get data for clipping
                    (
                        best_metric,
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
                    ) = self.run_prediction(
                        self.charge_limit_best,
                        self.charge_window_best,
                        self.export_window_best,
                        self.export_limits_best,
                        False,
                        end_record=self.end_record,
                    )

                    # Work out record windows
                    record_export_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.export_window_best), 1)

                    # Export slot clipping
                    self.export_window_best, self.export_limits_best = self.clip_export_slots(self.minutes_now, self.predict_soc, self.export_window_best, self.export_limits_best, record_export_windows, PREDICT_STEP)

                    # Filter out the windows we disabled during clipping
                    self.export_limits_best, self.export_window_best = self.discard_unused_export_slots(self.export_limits_best, self.export_window_best)
                self.log("Export windows filtered {}".format(self.window_as_text(self.export_window_best, self.export_limits_best)))

            # Filter out any unused charge slots
            if self.calculate_best_charge and self.charge_window_best:
                # Re-run prediction to get data for clipping
                (
                    best_metric,
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
                ) = self.run_prediction(
                    self.charge_limit_best,
                    self.charge_window_best,
                    self.export_window_best,
                    self.export_limits_best,
                    False,
                    end_record=self.end_record,
                )
                self.log("Raw charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max)), self.reserve))

                # Initial charge slot filter
                if self.set_charge_window:
                    record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
                    self.charge_limit_best, self.charge_window_best = self.discard_unused_charge_slots(self.charge_limit_best, self.charge_window_best, self.reserve)

                # Charge slot clipping
                record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
                self.log("Unclipped charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max)), self.reserve))
                self.charge_window_best, self.charge_limit_best = self.clip_charge_slots(self.minutes_now, self.predict_soc, self.charge_window_best, self.charge_limit_best, record_charge_windows, PREDICT_STEP)

                if self.set_charge_window:
                    # Filter out the windows we disabled during clipping
                    self.log("Unfiltered charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max)), self.reserve))
                    self.charge_limit_best, self.charge_window_best = self.discard_unused_charge_slots(self.charge_limit_best, self.charge_window_best, self.reserve)
                    self.charge_limit_percent_best = calc_percent_limit(self.charge_limit_best, self.soc_max)
                    self.log("Filtered charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max)), self.reserve))
                else:
                    self.charge_limit_percent_best = calc_percent_limit(self.charge_limit_best, self.soc_max)
                    self.log("Unfiltered charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max)), self.reserve))

            # Plan comparison
            if charge_window_best_prev is not None:
                # Run new plan
                (
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
                ) = self.run_prediction(
                    self.charge_limit_best,
                    self.charge_window_best,
                    self.export_window_best,
                    self.export_limits_best,
                    False,
                    end_record=self.end_record,
                )
                (
                    cost10,
                    import_kwh_battery10,
                    import_kwh_house10,
                    export_kwh10,
                    soc_min10,
                    soc10,
                    soc_min_minute10,
                    battery_cycle10,
                    metric_keep10,
                    final_iboost10,
                    final_carbon_g10,
                ) = self.run_prediction(
                    self.charge_limit_best,
                    self.charge_window_best,
                    self.export_window_best,
                    self.export_limits_best,
                    True,
                    end_record=self.end_record,
                )
                metric, battery_value = self.compute_metric(self.end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh)

                # Run previous plan
                (
                    cost_prev,
                    import_kwh_battery_prev,
                    import_kwh_house_prev,
                    export_kwh_prev,
                    soc_min_prev,
                    soc_prev,
                    soc_min_minute_prev,
                    battery_cycle_prev,
                    metric_keep_prev,
                    final_iboost_prev,
                    final_carbon_g_prev,
                ) = self.run_prediction(
                    charge_limit_best_prev,
                    charge_window_best_prev,
                    export_window_best_prev,
                    export_limits_best_prev,
                    False,
                    end_record=self.end_record,
                )
                (
                    cost10_prev,
                    import_kwh_battery10_prev,
                    import_kwh_house10_prev,
                    export_kwh_prev10_prev,
                    soc_min10_prev,
                    soc10_prev,
                    soc_min_minute10_prev,
                    battery_cycle10_prev,
                    metric_keep10_prev,
                    final_iboost10_prev,
                    final_carbon_g10_prev,
                ) = self.run_prediction(
                    charge_limit_best_prev,
                    charge_window_best_prev,
                    export_window_best_prev,
                    export_limits_best_prev,
                    True,
                    end_record=self.end_record,
                )
                metric_prev, battery_value = self.compute_metric(
                    self.end_record, soc_prev, soc10_prev, cost_prev, cost10_prev, final_iboost_prev, final_iboost10_prev, battery_cycle_prev, metric_keep_prev, final_carbon_g_prev, import_kwh_battery_prev, import_kwh_house_prev, export_kwh_prev
                )

                self.log("Previous plan best metric is {} (cost {}) and new plan best metric is {} (cost {})".format(dp2(metric_prev), dp2(cost_prev), dp2(metric), dp2(cost)))
                if metric_prev - metric < 0.1:
                    self.log("New plan metric is not significantly better than previous plan, using previous plan")
                    self.charge_window_best = copy.deepcopy(charge_window_best_prev)
                    self.charge_limit_best = copy.deepcopy(charge_limit_best_prev)
                    self.export_window_best = copy.deepcopy(export_window_best_prev)
                    self.export_limits_best = copy.deepcopy(export_limits_best_prev)
                else:
                    self.log("New plan metric is significantly better from previous plan, using new plan")

            # Plan is now valid
            self.log("Plan valid is now true after recompute was {}".format(self.plan_valid))
            self.plan_valid = True
            self.plan_last_updated = self.now_utc
            self.plan_last_updated_minutes = self.minutes_now

        # Final simulation of base
        metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
            self.charge_limit, self.charge_window, self.export_window, self.export_limits, False, save="base" if publish else None, end_record=self.end_record
        )
        # And base 10
        (
            metricb10,
            import_kwh_batteryb10,
            import_kwh_houseb10,
            export_kwhb10,
            soc_minb10,
            socb10,
            soc_min_minuteb10,
            battery_cycle10,
            metric_keep10,
            final_iboost10,
            final_carbon_g10,
        ) = self.run_prediction(
            self.charge_limit,
            self.charge_window,
            self.export_window,
            self.export_limits,
            True,
            save="base10" if publish else None,
            end_record=self.end_record,
        )

        if self.calculate_best:
            # Final simulation of best, do 10% and normal scenario
            (
                best_metric10,
                import_kwh_battery10,
                import_kwh_house10,
                export_kwh10,
                soc_min10,
                soc10,
                soc_min_minute10,
                battery_cycle10,
                metric_keep10,
                final_iboost10,
                final_carbon_g10,
            ) = self.run_prediction(
                self.charge_limit_best,
                self.charge_window_best,
                self.export_window_best,
                self.export_limits_best,
                True,
                save="best10" if publish else None,
                end_record=self.end_record,
            )
            (
                best_metric,
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
            ) = self.run_prediction(
                self.charge_limit_best,
                self.charge_window_best,
                self.export_window_best,
                self.export_limits_best,
                False,
                save="best" if publish else None,
                end_record=self.end_record,
            )
            # round charge_limit_best (kWh) to 2 decimal places and export_limits_best (percentage) to nearest whole number
            self.charge_limit_best = [dp2(elem) for elem in self.charge_limit_best]
            self.export_limits_best = [dp2(elem) for elem in self.export_limits_best]

            self.log(
                "Best charging limit socs {} export {} gives import battery {} house {} export {} metric {} metric10 {}".format(
                    self.charge_limit_best,
                    self.export_limits_best,
                    dp2(import_kwh_battery),
                    dp2(import_kwh_house),
                    dp2(export_kwh),
                    dp2(best_metric),
                    dp2(best_metric10),
                )
            )

            # Publish charge and export window best
            if publish:
                self.charge_limit_percent_best = calc_percent_limit(self.charge_limit_best, self.soc_max)
                self.publish_charge_limit(self.charge_limit_best, self.charge_window_best, self.charge_limit_percent_best, best=True, soc=self.predict_soc_best)
                self.publish_export_limit(self.export_window_best, self.export_limits_best, best=True)

                # HTML data
                self.publish_html_plan(pv_forecast_minute_step, pv_forecast_minute10_step, load_minutes_step, load_minutes_step10, self.end_record)

                # Web history
                if self.web_interface:
                    self.web_interface.history_update()

        # Destroy pool
        if self.pool:
            try:
                self.pool.close()
                self.pool.join()
            except Exception as e:
                self.log("Warn: failed to close thread pool: {}".format(e))
                self.log("Warn: " + traceback.format_exc())
            self.pool = None
        # Return if we recomputed or not
        return recompute

    def compute_metric(self, end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh):
        """
        Compute the metric combing pv and pv10 data
        """
        # Store simulated mid value
        metric = cost
        metric10 = cost10

        # Balancing payment to account for battery left over
        # ie. how much extra battery is worth to us in future, assume it's the same as low rate
        rate_min = (self.rate_min_forward.get(self.minutes_now + end_record, self.rate_min)) / self.inverter_loss / self.battery_loss + self.metric_battery_cycle
        rate_min = max(min(rate_min, self.rate_max * self.inverter_loss * self.battery_loss - self.metric_battery_cycle), 0)
        rate_export_min = self.rate_export_min * self.inverter_loss * self.battery_loss_discharge - self.metric_battery_cycle - rate_min
        battery_value = (soc * self.metric_battery_value_scaling + final_iboost * self.iboost_value_scaling) * max(rate_min, 1.0, rate_export_min)
        battery_value10 = (soc10 * self.metric_battery_value_scaling + final_iboost10 * self.iboost_value_scaling) * max(rate_min, 1.0, rate_export_min)
        metric -= battery_value
        metric10 -= battery_value10

        # Metric adjustment based on 10% outcome weighting
        if metric10 > metric:
            metric_diff = metric10 - metric
            metric_diff *= self.pv_metric10_weight
            metric += metric_diff
        else:
            metric_diff = 0

        # Carbon metric
        if self.carbon_enable:
            metric += (final_carbon_g / 1000) * self.carbon_metric

        # Self sufficiency metric
        metric += (import_kwh_house + import_kwh_battery) * self.metric_self_sufficiency

        # Adjustment for battery cycles metric
        metric += battery_cycle * self.metric_battery_cycle + metric_keep

        return dp4(metric), dp4(battery_value)

    def optimise_charge_limit(self, window_n, record_charge_windows, charge_limit, charge_window, export_window, export_limits, all_n=None, end_record=None):
        """
        Optimise a single charging window for best SoC
        """
        loop_soc = self.soc_max
        best_soc = self.soc_max
        best_soc_min = 0
        best_soc_min_minute = 0
        best_metric = 9999999
        best_cost = 0
        best_import = 0
        best_soc_step = self.best_soc_step
        best_keep = 0
        best_cycle = 0
        best_carbon = 0
        all_max_soc = self.soc_max
        all_min_soc = 0
        try_charge_limit = copy.deepcopy(charge_limit)
        resultmid = {}
        result10 = {}

        min_improvement_scaled = self.metric_min_improvement
        if not all_n:
            start = charge_window[window_n]["start"]
            end = charge_window[window_n]["end"]
            window_size = end - start
            if window_size <= 30:
                best_soc_step = best_soc_step * 2
            min_improvement_scaled = self.metric_min_improvement * window_size / 30.0

        # Start the loop at the max soc setting
        if self.best_soc_max > 0:
            loop_soc = min(loop_soc, self.best_soc_max)

        # Create min/max SoC to avoid simulating SoC that are not going have any impact
        # Can't do this for anything but a single window as the winder SoC impact isn't known
        if not all_n:
            hans = []
            all_max_soc = 0
            all_min_soc = self.soc_max
            hans.append(self.launch_run_prediction_charge(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, False, all_n, end_record))
            hans.append(self.launch_run_prediction_charge(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, True, all_n, end_record))
            hans.append(self.launch_run_prediction_charge(best_soc_min, window_n, charge_limit, charge_window, export_window, export_limits, False, all_n, end_record))
            hans.append(self.launch_run_prediction_charge(best_soc_min, window_n, charge_limit, charge_window, export_window, export_limits, True, all_n, end_record))
            id = 0
            for han in hans:
                (
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
                ) = han.get()
                all_min_soc = min(all_min_soc, min_soc)
                all_max_soc = max(all_max_soc, max_soc)
                if id == 0:
                    resultmid[loop_soc] = [
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
                    ]
                elif id == 1:
                    result10[loop_soc] = [
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
                    ]
                elif id == 2:
                    resultmid[best_soc_min] = [
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
                    ]
                elif id == 3:
                    result10[best_soc_min] = [
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
                    ]
                id += 1

        # Assemble list of SoC's to try
        try_socs = [loop_soc]
        loop_step = max(best_soc_step, 0.1)
        best_soc_min_setting = self.best_soc_min
        if best_soc_min_setting > 0:
            best_soc_min_setting = max(self.reserve, best_soc_min_setting)

        while loop_soc > self.reserve:
            skip = False
            try_soc = max(best_soc_min, loop_soc)
            try_soc = dp2(min(try_soc, self.soc_max))
            if try_soc > (all_max_soc + loop_step):
                skip = True
            if (try_soc > self.reserve) and (try_soc > self.best_soc_min) and (try_soc < (all_min_soc - loop_step)):
                skip = True
            # Keep those we already simulated
            if try_soc in resultmid:
                skip = False
            # Keep the current setting if different from the selected ones
            if not all_n and try_soc == charge_limit[window_n]:
                skip = False
            # All to the list
            if not skip and (try_soc not in try_socs) and (try_soc != self.reserve):
                try_socs.append(dp2(try_soc))
            loop_soc -= loop_step

        # Give priority to off to avoid spurious charge freezes
        if best_soc_min_setting not in try_socs:
            try_socs.append(best_soc_min_setting)
        if self.set_charge_freeze and (self.reserve not in try_socs):
            try_socs.append(self.reserve)
        if not self.set_charge_freeze and (self.reserve in try_socs):
            try_socs.remove(self.reserve)

        # Run the simulations in parallel
        results = []
        results10 = []
        for try_soc in try_socs:
            if try_soc not in resultmid:
                hanres = self.launch_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, export_window, export_limits, False, all_n, end_record)
                results.append(hanres)
                hanres10 = self.launch_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, export_window, export_limits, True, all_n, end_record)
                results10.append(hanres10)

        # Get results from sims if we simulated them
        for try_soc in try_socs:
            if try_soc not in resultmid:
                hanres = results.pop(0)
                hanres10 = results10.pop(0)
                resultmid[try_soc] = hanres.get()
                result10[try_soc] = hanres10.get()

        window_results = {}
        # Now we have all the results, we can pick the best SoC
        for try_soc in try_socs:
            window = charge_window[window_n]

            # Store try value into the window, either all or just this one
            if all_n:
                for window_id in all_n:
                    try_charge_limit[window_id] = try_soc
            else:
                try_charge_limit[window_n] = try_soc

            # Simulate with medium PV
            (
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
            ) = resultmid[try_soc]
            (
                cost10,
                import_kwh_battery10,
                import_kwh_house10,
                export_kwh10,
                soc_min10,
                soc10,
                soc_min_minute10,
                battery_cycle10,
                metric_keep10,
                final_iboost10,
                final_carbon_g10,
                min_soc10,
                max_soc10,
            ) = result10[try_soc]

            # Compute the metric from simulation results
            metric, battery_value = self.compute_metric(end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh)

            # Metric adjustment based on current charge limit when inside the window
            # to try to avoid constant small changes to SoC target by forcing to keep the current % during a charge period
            # if changing it has little impact
            if not all_n and self.isCharging and (window_n == self.in_charge_window(charge_window, self.minutes_now)):
                if calc_percent_limit(self.isCharging_Target, self.soc_max) == calc_percent_limit(try_soc, self.soc_max):
                    metric -= max(0.1, self.metric_min_improvement)

            if try_soc == best_soc_min_setting:
                # Minor weighting to 0%
                metric -= 0.003
            elif try_soc == self.soc_max:
                # Minor weighting to 100%
                metric -= 0.002
            elif self.set_charge_freeze and try_soc == self.reserve:
                # Minor weighting to freeze
                metric -= 0.001

            # Round metric to 4 DP
            metric = dp4(metric)

            if self.debug_enable:
                self.log(
                    "Sim: SoC {} soc_min {} @ {} window {} metric {} cost {} cost10 {} soc {} soc10 {} final_iboost {} final_iboost10 {} final_carbon_g {} metric_keep {} cycle {} carbon {} import {} export {} battery_value {}".format(
                        try_soc,
                        dp4(soc_min),
                        self.time_abs_str(soc_min_minute),
                        window_n,
                        metric,
                        cost,
                        cost10,
                        soc,
                        soc10,
                        final_iboost,
                        final_iboost10,
                        final_carbon_g,
                        metric_keep,
                        battery_cycle,
                        final_carbon_g,
                        import_kwh_battery + import_kwh_house,
                        export_kwh,
                        battery_value,
                    )
                )

            window_results[try_soc] = metric

            # Only select the lower SoC if it makes a notable improvement has defined by min_improvement (divided in M windows)
            # and it doesn't fall below the soc_keep threshold
            if (metric + min_improvement_scaled) <= best_metric:
                best_metric = metric
                best_soc = try_soc
                best_cost = cost
                best_soc_min = soc_min
                best_soc_min_minute = soc_min_minute
                best_keep = metric_keep
                best_cycle = battery_cycle
                best_carbon = final_carbon_g
                best_import = import_kwh_battery + import_kwh_house

        # Add margin last
        best_soc = min(best_soc + self.best_soc_margin, self.soc_max)

        if self.debug_enable:
            if not all_n:
                self.log(
                    "Try optimising charge window(s)    {}: {} - {} price {} cost {} metric {} keep {} cycle {} carbon {} import {} export {} selected {} was {} results {}".format(
                        window_n,
                        self.time_abs_str(window["start"]),
                        self.time_abs_str(window["end"]),
                        charge_window[window_n]["average"],
                        dp4(best_cost),
                        dp4(best_metric),
                        dp4(best_keep),
                        dp4(best_cycle),
                        dp0(best_carbon),
                        dp4(import_kwh_battery + import_kwh_house),
                        dp4(export_kwh),
                        best_soc,
                        charge_limit[window_n],
                        window_results,
                    )
                )
            else:
                self.log(
                    "Try optimising charge window(s)    {}: price {} cost {} metric {} keep {} cycle {} carbon {} import {} selected {} was {} results {}".format(
                        all_n,
                        charge_window[window_n]["average"],
                        dp2(best_cost),
                        dp2(best_metric),
                        dp2(best_keep),
                        dp2(best_cycle),
                        dp0(best_carbon),
                        dp0(best_import),
                        best_soc,
                        charge_limit[window_n],
                        window_results,
                    )
                )
        return best_soc, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep, best_cycle, best_carbon, best_import

    def optimise_export(self, window_n, record_charge_windows, try_charge_limit, charge_window, export_window, export_limit, all_n=None, end_record=None, freeze_only=False):
        """
        Optimise a single export window for best export %
        """
        best_export = False
        best_metric = 9999999
        off_metric = 9999999
        best_cost = 0
        best_soc_min = 0
        best_soc_min_minute = 0
        best_keep = 0
        best_cycle = 0
        best_import = 0
        best_carbon = 0
        this_export_limit = 100.0
        window = export_window[window_n]
        try_export_window = copy.deepcopy(export_window)
        try_export = copy.deepcopy(export_limit)
        best_start = window["start"]
        best_size = window["end"] - best_start
        export_step = 5

        # loop on each export option
        if self.set_export_freeze and (freeze_only or self.set_export_freeze_only):
            loop_options = [100, 99]
        elif self.set_export_freeze and not self.set_export_freeze_only:
            # If we support freeze, try a 99% option which will freeze at any SoC level below this
            loop_options = [100, 99, 0]
            if self.set_export_low_power:
                loop_options.extend([0.3, 0.5, 0.7])
        else:
            loop_options = [100, 0]
            if self.set_export_low_power:
                loop_options.extend([0.3, 0.5, 0.7])

        # Collect all options
        results = []
        results10 = []
        try_options = []
        for loop_limit in loop_options:
            # Loop on window size
            loop_start = window["end"] - 5  # Minimum export window size 5 minutes
            while loop_start >= window["start"]:
                this_export_limit = loop_limit
                start = loop_start

                # Move the loop start back to full size
                loop_start -= export_step

                # Can't optimise all window start slot
                if all_n and (start != window["start"]):
                    continue

                # Don't allow slow export for small windows
                if this_export_limit > int(this_export_limit) and (try_export_window[window_n]["end"] - start) < 15:
                    continue

                # Don't optimise start of disabled windows or freeze only windows, just for export ones
                if (this_export_limit in [100.0, 99.0]) and (start != window["start"]):
                    continue

                # Never go below the minimum level
                this_export_limit = max(calc_percent_limit(max(self.best_soc_min, self.reserve), self.soc_max), int(this_export_limit))
                this_export_limit = this_export_limit + loop_limit - int(loop_limit)
                try_options.append([start, this_export_limit])

                results.append(self.launch_run_prediction_export(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, False, all_n, end_record))
                results10.append(self.launch_run_prediction_export(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, True, all_n, end_record))

        # Get results from sims
        try_results = []
        for try_option in try_options:
            hanres = results.pop(0)
            hanres10 = results10.pop(0)
            result = hanres.get()
            result10 = hanres10.get()
            try_results.append(try_option + [result, result10])

        window_results = {}
        for try_option in try_results:
            start, this_export_limit, hanres, hanres10 = try_option

            # Simulate with medium PV
            cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = hanres
            (
                cost10,
                import_kwh_battery10,
                import_kwh_house10,
                export_kwh10,
                soc_min10,
                soc10,
                soc_min_minute10,
                battery_cycle10,
                metric_keep10,
                final_iboost10,
                final_carbon_g10,
            ) = hanres10

            # Compute the metric from simulation results
            metric, battery_value = self.compute_metric(end_record, soc, soc10, cost, cost10, final_iboost, final_iboost10, battery_cycle, metric_keep, final_carbon_g, import_kwh_battery, import_kwh_house, export_kwh)

            if this_export_limit == 100:
                # Minor weighting to off
                metric -= 0.002
            elif this_export_limit == 0:
                # Minor weighting to 0%
                metric -= 0.001

            # Adjust to try to keep existing windows
            if window_n < 2 and this_export_limit < 99.0 and self.export_window and self.isExporting:
                pwindow = export_window[window_n]
                dwindow = self.export_window[0]
                if self.minutes_now >= pwindow["start"] and self.minutes_now < pwindow["end"] and ((self.minutes_now >= dwindow["start"] and self.minutes_now < dwindow["end"]) or (dwindow["end"] == pwindow["start"])):
                    metric -= max(0.5, self.metric_min_improvement_export)

            # Round metric to 4 DP
            metric = dp4(metric)

            if self.debug_enable:
                self.log(
                    "Sim: Export {} window {} start {} end {}, import {} export {} min_soc {} @ {} soc {} soc10 {} cost {} cost10 {} metric {} cycle {} iboost {} iboost10 {} carbon {} keep {} battery_value {} end_record {}".format(
                        this_export_limit,
                        window_n,
                        self.time_abs_str(start),
                        self.time_abs_str(try_export_window[window_n]["end"]),
                        dp4(import_kwh_battery + import_kwh_house),
                        dp4(export_kwh),
                        dp4(soc_min),
                        self.time_abs_str(soc_min_minute),
                        dp4(soc),
                        dp4(soc10),
                        dp4(cost),
                        dp4(cost10),
                        dp4(metric),
                        dp4(battery_cycle * self.metric_battery_cycle),
                        dp4(final_iboost),
                        dp4(final_iboost10),
                        dp4(final_carbon_g),
                        dp4(metric_keep),
                        battery_value,
                        end_record,
                    )
                )

            window_size = try_export_window[window_n]["end"] - start
            window_key = str(dp2(this_export_limit)) + "_" + str(window_size)
            window_results[window_key] = [metric, cost]

            # Only select an export if it makes a notable improvement has defined by min_improvement (divided in M windows)
            # Scale back in the case of freeze export as improvements will be smaller
            rate_scale = 1 - (this_export_limit - int(this_export_limit))

            if this_export_limit == 99:
                min_improvement_scaled = self.metric_min_improvement_export_freeze
            elif all_n:
                min_improvement_scaled = self.metric_min_improvement_export * rate_scale * len(all_n)
            else:
                min_improvement_scaled = self.metric_min_improvement_export * window_size * rate_scale / 30.0

            # Only select an export if it makes a notable improvement has defined by min_improvement (divided in M windows)
            if ((metric + min_improvement_scaled) <= off_metric) and (metric <= best_metric):
                best_metric = metric
                best_export = this_export_limit
                best_cost = cost
                best_soc_min = soc_min
                best_soc_min_minute = soc_min_minute
                best_start = start
                best_size = window_size
                best_keep = metric_keep
                best_cycle = battery_cycle
                best_carbon = final_carbon_g
                best_import = import_kwh_battery + import_kwh_house

            # Store the metric for export off
            if off_metric == 9999999:
                off_metric = metric

        if self.debug_enable:
            if not all_n:
                self.log(
                    "Try optimising export window(s) {}: {} - {} price {} cost {} metric {} carbon {} import {} keep {} selected {}% size {} was {}% results {}".format(
                        window_n,
                        self.time_abs_str(window["start"]),
                        self.time_abs_str(window["end"]),
                        window["average"],
                        dp4(best_cost),
                        dp4(best_metric),
                        dp4(best_carbon),
                        dp4(best_import),
                        dp4(best_keep),
                        best_export,
                        best_size,
                        export_limit[window_n],
                        window_results,
                    )
                )
            else:
                self.log(
                    "Try optimising export window(s) {} price {} selected {}% size {} cost {} metric {} carbon {} import {} keep {} results {}".format(
                        all_n,
                        window["average"],
                        dp4(best_cost),
                        dp4(best_metric),
                        dp4(best_carbon),
                        dp4(best_import),
                        dp4(best_keep),
                        best_export,
                        best_size,
                        window_results,
                    )
                )

        return best_export, best_start, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep, best_cycle, best_carbon, best_import

    def window_sort_func(self, window):
        """
        Helper sort index function
        """
        return float(window["key"])

    def window_sort_func_start(self, window):
        """
        Helper sort index function
        """
        return float(window["start"])

    def sort_window_by_time(self, windows):
        """
        Sort windows in start time order, return a new list of windows
        """
        window_sorted = copy.deepcopy(windows)
        window_sorted.sort(key=self.window_sort_func_start)
        return window_sorted

    def sort_window_by_price_combined(self, charge_windows, export_windows, calculate_import_low_export=False, calculate_export_high_import=False):
        """
        Sort windows into price sets
        """
        window_sort = []
        window_links = {}
        price_set = []
        price_links = {}

        # Add charge windows
        if self.calculate_best_charge:
            id = 0
            for window in charge_windows:
                # Account for losses in average rate as it makes import higher
                average = dp2(window["average"] / self.inverter_loss / self.battery_loss + self.metric_battery_cycle)
                if self.carbon_enable:
                    carbon_intensity = self.carbon_intensity.get(window["start"] - self.minutes_now, 0)
                    average += carbon_intensity * self.carbon_metric / 1000.0
                average += self.metric_self_sufficiency
                if calculate_import_low_export:
                    average_export = dp2((self.rate_export.get(window["start"], 0) + self.rate_export.get(window["end"] - PREDICT_STEP, 0)) / 2)
                else:
                    average_export = 0
                window_start = window["start"]
                sort_key = "%04.2f_%04.2f_%04d_c%02d" % (5000 - average, 5000 - average_export, 9999 - window_start, id)
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "c"
                window_links[sort_key]["id"] = id
                window_links[sort_key]["average"] = dp1(average)  # Round to nearest 0.1 penny to avoid too many bands
                window_links[sort_key]["average_secondary"] = dp1(average_export)  # Round to nearest 0.1 penny to avoid too many bands
                id += 1

        # Add export windows
        if self.calculate_best_export:
            id = 0
            for window in export_windows:
                # Account for losses in average rate as it makes export value lower
                average = dp2(window["average"] * self.inverter_loss * self.battery_loss_discharge - self.metric_battery_cycle)
                if self.carbon_enable:
                    carbon_intensity = self.carbon_intensity.get(window["start"] - self.minutes_now, 0)
                    average += carbon_intensity * self.carbon_metric / 1000.0
                if calculate_export_high_import:
                    average_import = dp2((self.rate_import.get(window["start"], 0) + self.rate_import.get(window["end"] - PREDICT_STEP, 0)) / 2)
                else:
                    average_import = 0
                window_start = window["start"]
                sort_key = "%04.2f_%04.2f_%04d_d%02d" % (5000 - average, 5000 - average_import, 9999 - window_start, id)
                if not self.calculate_export_first:
                    # Push export last if first is not set
                    sort_key = "zz_" + sort_key
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "d"
                window_links[sort_key]["id"] = id
                window_links[sort_key]["average"] = dp1(average)  # Round to nearest 0.1 penny to avoid too many bands
                window_links[sort_key]["average_secondary"] = dp1(average_import)  # Round to nearest 0.1 penny to avoid too many bands
                id += 1

        if window_sort:
            window_sort.sort()

        # Create price ordered links by set
        for key in window_sort:
            average = window_links[key]["average"]
            if average not in price_set:
                price_set.append(average)
                price_links[average] = []
            price_links[average].append(key)

        return window_sort, window_links, price_set, price_links

    def sort_window_by_time_combined(self, charge_windows, export_windows):
        window_sort = []
        window_links = {}

        # Add charge windows
        if self.calculate_best_charge:
            id = 0
            for window in charge_windows:
                sort_key = "%04d_%03d_c" % (window["start"], id)
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "c"
                window_links[sort_key]["id"] = id
                id += 1

        # Add export windows
        if self.calculate_best_export:
            id = 0
            for window in export_windows:
                sort_key = "%04d_%03d_d" % (window["start"], id)
                window_sort.append(sort_key)
                window_links[sort_key] = {}
                window_links[sort_key]["type"] = "d"
                window_links[sort_key]["id"] = id
                id += 1

        if window_sort:
            window_sort.sort()

        return window_sort, window_links

    def sort_window_by_price(self, windows, reverse_time=False):
        """
        Sort the charge windows by highest price first, return a list of window IDs
        """
        window_with_id = copy.deepcopy(windows)
        wid = 0
        for window in window_with_id:
            window["id"] = wid
            if reverse_time:
                window["key"] = "%04.2f%02d" % (5000 - window["average"], 999 - window["id"])
            else:
                window["key"] = "%04.2f%02d" % (5000 - window["average"], window["id"])
            wid += 1
        window_with_id.sort(key=self.window_sort_func)
        id_list = []
        for window in window_with_id:
            id_list.append(window["id"])
        return id_list

    def discard_unused_charge_slots(self, charge_limit_best, charge_window_best, reserve):
        """
        Filter out unused charge slots (those set at 0)
        """
        new_limit_best = []
        new_window_best = []

        max_slots = len(charge_limit_best)

        for window_n in range(max_slots):
            # Only keep slots > than reserve, or keep the last one so we don't have zero slots
            # Also keep a slot if we are already inside it and charging is enabled
            window = charge_window_best[window_n].copy()
            start = window["start"]
            end = window["end"]
            limit = charge_limit_best[window_n]

            predict_minute_start = max(int((start - self.minutes_now) / 5) * 5, 0)
            predict_minute_end = int((end - self.minutes_now) / 5) * 5
            window["target"] = self.predict_soc.get(predict_minute_end, limit)

            if (
                new_window_best
                and (start == new_window_best[-1]["end"])
                and (limit == new_limit_best[-1])
                and (start not in self.manual_all_times)
                and (new_window_best[-1]["start"] not in self.manual_all_times)
                and (new_window_best[-1]["average"] >= window["average"] or not self.set_charge_low_power)
            ):
                # Combine two windows of the same charge target provided the rates are the same or low power mode is off (low power mode can skew the charge into the more expensive slot)
                new_window_best[-1]["end"] = end
                new_window_best[-1]["target"] = window.get("target", limit)
                new_window_best[-1]["average"] = (new_window_best[-1]["average"] + window["average"]) / 2
                if self.debug_enable:
                    self.log("Combine charge slot {} with previous (same target) - target soc {} kWh slot {} start {} end {} limit {}".format(window_n, new_limit_best[-1], new_window_best[-1], start, end, limit))
            elif (
                new_window_best
                and (start == new_window_best[-1]["end"])
                and (limit >= new_limit_best[-1])
                and not (limit != self.reserve and new_limit_best[-1] == self.reserve)
                and (start not in self.manual_all_times)
                and (new_window_best[-1]["start"] not in self.manual_all_times)
                and new_window_best[-1]["average"] == window["average"]
                and (new_window_best[-1]["target"] < new_limit_best[-1])
            ):
                # Combine two windows of the same price, provided the second charge limit is greater than the first
                # and the old charge never reaches it defined limit
                new_window_best[-1]["end"] = end
                new_window_best[-1]["target"] = window.get("target", limit)
                new_limit_best[-1] = limit
                if self.debug_enable:
                    self.log("Combine charge slot {} with previous (same price) - target soc {} kWh slot {} start {} end {} limit {}".format(window_n, new_limit_best[-1], new_window_best[-1], start, end, limit))
            elif limit > 0:
                new_limit_best.append(limit)
                new_window_best.append(window)
            else:
                if self.debug_enable:
                    self.log("Clip off charge slot {} limit {}".format(window, limit))
        return new_limit_best, new_window_best

    def find_spare_energy(self, predict_soc, predict_export, step, first_charge):
        """
        Find spare energy and set triggers
        """
        triggers = self.args.get("export_triggers", [])
        if not isinstance(triggers, list):
            return

        # Only run if we have export data
        if not predict_export:
            return

        # Check each trigger
        for trigger in triggers:
            total_energy = 0
            name = trigger.get("name", "trigger")
            minutes = trigger.get("minutes", 60.0)
            minutes = min(max(minutes, 0), first_charge)
            energy = trigger.get("energy", 1.0)
            try:
                energy = float(energy)
            except (ValueError, TypeError):
                energy = 0.0
                self.log("Warn: Bad energy value {} provided via trigger {}".format(energy, name))
                self.record_status("Error: Bad energy value {} provided via trigger {}".format(energy, name), had_errors=True)

            for minute in range(0, minutes, step):
                total_energy += predict_export[minute]
            sensor_name = "binary_sensor." + self.prefix + "_export_trigger_" + name
            if total_energy >= energy:
                state = "on"
            else:
                state = "off"
            self.log("Evaluate trigger {} results {} total_energy {}".format(trigger, state, dp2(total_energy)))
            self.dashboard_item(
                sensor_name,
                state=state,
                attributes={
                    "friendly_name": "Predbat export trigger " + name,
                    "required": energy,
                    "available": dp2(total_energy),
                    "minutes": minutes,
                    "icon": "mdi:clock-start",
                },
            )

    def clip_charge_slots(self, minutes_now, predict_soc, charge_window_best, charge_limit_best, record_charge_windows, step):
        """
        Clip charge slots that are useless as they don't charge at all
        set the 'target' field in the charge window for HTML reporting
        """
        for window_n in range(min(record_charge_windows, len(charge_window_best))):
            window = charge_window_best[window_n]
            limit = charge_limit_best[window_n]
            limit_percent = calc_percent_limit(limit, self.soc_max)
            window_start = max(window["start"], minutes_now)
            window_end = max(window["end"], minutes_now)
            window_length = window_end - window_start
            window["target"] = limit

            if limit <= 0.0:
                # Ignore disabled windows
                pass
            elif window_length > 0:
                predict_minute_start = max(int((window_start - minutes_now) / 5) * 5, 0)
                predict_minute_end = int((window_end - minutes_now) / 5) * 5
                predict_minute_end_m1 = max(predict_minute_end - 5, predict_minute_start)

                if (predict_minute_start in predict_soc) and (predict_minute_end in predict_soc):
                    # Work out min/max soc
                    soc_min = self.soc_max
                    soc_max = 0
                    for minute in range(predict_minute_start, predict_minute_end + 5, 5):
                        if minute in predict_soc:
                            soc_min = min(soc_min, predict_soc[minute])
                            soc_max = max(soc_max, predict_soc[minute])

                    soc_m1 = predict_soc[predict_minute_end_m1]
                    soc_min_percent = calc_percent_limit(soc_min, self.soc_max)

                    if self.debug_enable:
                        self.log("Examine charge window {} from {} - {} (minute {}) limit {} - min soc {} max soc {} soc_m1 {}".format(window_n, window_start, window_end, predict_minute_start, limit, soc_min, soc_max, soc_m1))

                    if (soc_min_percent > (limit_percent + 1)) and (limit != self.reserve):
                        charge_limit_best[window_n] = 0
                        window["target"] = 0
                        if self.debug_enable:
                            self.log("Clip off charge window {} from {} - {} from limit {} to new limit {} min percent {} limit percent {}".format(window_n, window_start, window_end, limit, charge_limit_best[window_n], soc_min_percent, limit_percent))
                    elif soc_max < limit:
                        # Work out what can be achieved in the window and set the target to match that
                        window["target"] = soc_max
                        charge_limit_best[window_n] = self.soc_max
                        if self.debug_enable:
                            self.log("Clip up charge window {} from {} - {} from limit {} to new limit {} target set to {}".format(window_n, window_start, window_end, limit, charge_limit_best[window_n], window["target"]))
                    elif (soc_max > soc_m1) and soc_max == limit:
                        window["target"] = soc_max
                        charge_limit_best[window_n] = self.soc_max
                        if self.debug_enable:
                            self.log("Clip up charge window {} from {} - {} from limit {} to new limit {} target set to {}".format(window_n, window_start, window_end, limit, charge_limit_best[window_n], window["target"]))

            else:
                self.log("Warn: Clip charge window {} as it's already passed".format(window_n))
                charge_limit_best[window_n] = 0
                window["target"] = 0
        return charge_window_best, charge_limit_best

    def clip_export_slots(self, minutes_now, predict_soc, export_window_best, export_limits_best, record_export_windows, step):
        """
        Clip export slots to the right length
        """
        for window_n in range(min(record_export_windows, len(export_window_best))):
            window = export_window_best[window_n]
            limit = export_limits_best[window_n]
            limit_soc = self.soc_max * limit / 100.0
            window_start = max(window["start"], minutes_now)
            window_end = max(window["end"], minutes_now)
            window_length = window_end - window_start
            window["target"] = limit

            if limit == 100 or limit == 99:
                # Ignore disabled windows & export freeze slots
                pass
            elif window_length > 0:
                predict_minute_start = max(int((window_start - minutes_now) / 5) * 5, 0)
                predict_minute_end = int((window_end - minutes_now) / 5) * 5
                if (predict_minute_start in predict_soc) and (predict_minute_end in predict_soc):
                    soc_min = self.soc_max
                    soc_max = 0
                    for minute in range(predict_minute_start, predict_minute_end + 5, 5):
                        if minute in predict_soc:
                            soc_min = min(soc_min, predict_soc[minute])
                            soc_max = max(soc_max, predict_soc[minute])

                    if self.debug_enable:
                        self.log("Examine window {} from {} - {} (minute {}) limit {} - starting soc {} ending soc {}".format(window_n, window_start, window_end, predict_minute_start, limit, soc_min, soc_max))

                    # Export level adjustments for safety
                    if soc_min > limit_soc:
                        # Give it 10 minute margin
                        target_soc = max(limit_soc, soc_min)
                        limit_soc = max(limit_soc, soc_min - 10 * self.battery_rate_max_discharge_scaled)
                        window["target"] = calc_percent_limit(target_soc, self.soc_max)
                        export_limits_best[window_n] = calc_percent_limit(limit_soc, self.soc_max) + (limit - int(limit))
                        if limit != export_limits_best[window_n] and self.debug_enable:
                            self.log("Clip up export window {} from {} - {} from limit {} to new limit {} target set to {}".format(window_n, window_start, window_end, limit, export_limits_best[window_n], window["target"]))
                    elif soc_max < limit_soc:
                        # Clip off the window
                        window["target"] = 100
                        export_limits_best[window_n] = 100
                        if self.debug_enable:
                            self.log("Clip off export window {} from {} - {} from limit {} to new limit {}".format(window_n, window_start, window_end, limit, export_limits_best[window_n]))
            else:
                self.log("Warn: Clip export window {} as it's already passed".format(window_n))
                export_limits_best[window_n] = 100
        return export_window_best, export_limits_best

    def discard_unused_export_slots(self, export_limits_best, export_window_best):
        """
        Filter out the windows we disabled
        """
        new_best = []
        new_enable = []
        for window_n in range(len(export_limits_best)):
            if export_limits_best[window_n] < 100.0:
                # Also merge contiguous enabled windows
                if (
                    new_best
                    and (export_window_best[window_n]["start"] == new_best[-1]["end"])
                    and (export_limits_best[window_n] == new_enable[-1])
                    and (export_window_best[window_n]["start"] not in self.manual_all_times)
                    and (new_best[-1]["start"] not in self.manual_all_times)
                ):
                    new_best[-1]["end"] = export_window_best[window_n]["end"]
                    new_best[-1]["target"] = export_window_best[window_n].get("target", export_limits_best[window_n])
                    if self.debug_enable:
                        self.log("Combine export slot {} with previous - percent {} slot {}".format(window_n, new_enable[-1], new_best[-1]))
                else:
                    new_best.append(copy.deepcopy(export_window_best[window_n]))
                    new_enable.append(export_limits_best[window_n])

        return new_enable, new_best

    def update_target_values(self):
        """
        Update target values for HTML plan
        """
        for window_n in range(len(self.export_limits_best)):
            self.export_window_best[window_n]["target"] = self.export_limits_best[window_n]
        for window_n in range(len(self.charge_limit_best)):
            self.charge_window_best[window_n]["target"] = self.charge_limit_best[window_n]

    def tweak_plan(self, end_record, best_metric, metric_keep):
        """
        Tweak existing plan only
        """
        record_charge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.charge_window_best), 1)
        record_export_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.export_window_best), 1)
        self.log("Tweak optimisation started")
        best_soc = self.soc_max
        best_cost = best_metric
        best_keep = metric_keep
        best_cycle = 0
        best_carbon = 0
        best_import = 0
        count = 0
        window_sorted, window_index = self.sort_window_by_time_combined(self.charge_window_best[:record_charge_windows], self.export_window_best[:record_export_windows])
        for key in window_sorted:
            typ = window_index[key]["type"]
            window_n = window_index[key]["id"]
            if typ == "c":
                # Don't optimise a charge window that hits an export window if this is disallowed
                if not self.calculate_export_oncharge:
                    hit_export = self.hit_charge_window(self.export_window_best, self.charge_window_best[window_n]["start"], self.charge_window_best[window_n]["end"])
                    if hit_export >= 0 and self.export_limits_best[hit_export] < 100:
                        continue

                window_start = self.charge_window_best[window_n]["start"]
                if self.calculate_best_charge and (window_start not in self.manual_all_times):
                    best_soc, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon, best_import = self.optimise_charge_limit(
                        window_n,
                        record_charge_windows,
                        self.charge_limit_best,
                        self.charge_window_best,
                        self.export_window_best,
                        self.export_limits_best,
                        end_record=end_record,
                    )
                    self.charge_limit_best[window_n] = best_soc
            else:
                window_start = self.export_window_best[window_n]["start"]
                if self.calculate_best_export and (window_start not in self.manual_all_times):
                    if not self.calculate_export_oncharge:
                        hit_charge = self.hit_charge_window(self.charge_window_best, self.export_window_best[window_n]["start"], self.export_window_best[window_n]["end"])
                        if hit_charge >= 0 and self.charge_limit_best[hit_charge] > 0.0:
                            continue
                    if not self.iboost_on_export and self.iboost_enable and self.iboost_plan and (self.hit_charge_window(self.iboost_plan, self.export_window_best[window_n]["start"], self.export_window_best[window_n]["end"]) >= 0):
                        continue
                    if not self.car_charging_from_battery and self.hit_car_window(self.export_window_best[window_n]["start"], self.export_window_best[window_n]["end"]):
                        continue

                    best_soc, best_start, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon, best_import = self.optimise_export(
                        window_n,
                        record_export_windows,
                        self.charge_limit_best,
                        self.charge_window_best,
                        self.export_window_best,
                        self.export_limits_best,
                        end_record=end_record,
                    )
                    self.export_limits_best[window_n] = best_soc
                    self.export_window_best[window_n]["start"] = best_start
            count += 1
            if count >= 8:
                break

        self.log("Tweak optimisation finished metric {} cost {} metric_keep {} cycle {} carbon {} import {}".format(dp2(best_metric), dp2(best_cost), dp2(best_keep), dp2(best_cycle), dp0(best_carbon), dp2(best_import)))

    def optimise_all_windows(self, best_metric, metric_keep, debug_mode=False):
        """
        Optimise all windows, both charge and export in rate order
        """
        record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
        record_export_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.export_window_best), 1)
        window_sorted, window_index, price_set, price_links = self.sort_window_by_price_combined(self.charge_window_best[:record_charge_windows], self.export_window_best[:record_export_windows])
        if debug_mode:
            price_order = []
            for price_key in price_set:
                links = price_links[price_key]
                for key in links:
                    typ = window_index[key]["type"]
                    window_n = window_index[key]["id"]
                    price = window_index[key]["average"]
                    if typ == "c":
                        real_price = self.charge_window_best[window_n]["average"]
                        price_order.append(real_price)

        best_soc = self.soc_max
        best_cost = best_metric
        best_keep = metric_keep
        best_cycle = 0
        best_carbon = 0
        best_import = 0
        best_price = 0
        best_price_export = 0
        fast_mode = True

        # Optimise all windows by picking a price threshold default
        if price_set and self.calculate_best_charge and self.charge_window_best:
            self.log("Optimise all windows, total charge {} export {}".format(record_charge_windows, record_export_windows))
            self.optimise_charge_windows_reset(reset_all=True)
            self.optimise_charge_windows_manual()
            (
                self.charge_limit_best,
                self.export_limits_best,
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
            ) = self.optimise_charge_limit_price_threads(
                price_set,
                price_links,
                window_index,
                record_charge_windows,
                record_export_windows,
                self.charge_limit_best,
                self.charge_window_best,
                self.export_window_best,
                self.export_limits_best,
                end_record=self.end_record,
                fast=fast_mode,
                quiet=False if debug_mode else True,
            )

            if debug_mode:
                metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
                    self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, False, end_record=self.end_record, save="best"
                )
                self.charge_limit_percent_best = calc_percent_limit(self.charge_limit_best, self.soc_max)
                self.publish_html_plan(self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)
                open("plan_pre_levels.html", "w").write(self.html_plan)
                print("Wrote plan to plan_pre_levels.html - metric {} cost {} battery_value {} keep {} import {} (self {})".format(best_metric, best_cost, best_battery_value, best_keep, best_import, best_import * self.metric_self_sufficiency))

            if self.calculate_regions:
                region_size = int(16 * 60)
                min_region_size = int(60)
                while region_size >= min_region_size:
                    self.log(">> Region optimisation pass width {}".format(region_size))
                    step_size = int(max(region_size / 2, min_region_size))
                    for region in range(0, self.end_record + self.minutes_now, step_size):
                        region_start = max(self.end_record + self.minutes_now - region - region_size, 0)
                        region_end = min(region_start + region_size, self.end_record + self.minutes_now)

                        if region_end < self.minutes_now:
                            continue

                        (
                            self.charge_limit_best,
                            self.export_limits_best,
                            ignore_best_price,
                            ignore_best_price_export,
                            best_metric,
                            best_cost,
                            best_keep,
                            best_soc_min,
                            best_cycle,
                            best_carbon,
                            best_import,
                            best_battery_value,
                            tried_list,
                        ) = self.optimise_charge_limit_price_threads(
                            price_set,
                            price_links,
                            window_index,
                            record_charge_windows,
                            record_export_windows,
                            self.charge_limit_best,
                            self.charge_window_best,
                            self.export_window_best,
                            self.export_limits_best,
                            end_record=self.end_record,
                            region_start=region_start,
                            region_end=region_end,
                            fast=fast_mode,
                            quiet=True,
                            best_metric=best_metric,
                            best_cost=best_cost,
                            best_keep=best_keep,
                            best_soc_min=best_soc_min,
                            best_price_charge=best_price,
                            best_price_export=best_price_export,
                            best_cycle=best_cycle,
                            best_import=best_import,
                            best_carbon=best_carbon,
                            best_battery_value=best_battery_value,
                            tried_list=tried_list,
                        )
                        # Reached the end of the window
                        if self.end_record + self.minutes_now - region - region_size < 0:
                            break

                    if debug_mode:
                        org_export_limits = self.export_limits_best
                        metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
                            self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, False, end_record=self.end_record, save="best"
                        )
                        self.charge_limit_percent_best = calc_percent_limit(self.charge_limit_best, self.soc_max)
                        self.update_target_values()
                        self.publish_html_plan(self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)
                        open("plan_levels_{}.html".format(region_size), "w").write(self.html_plan)
                        print(
                            "Wrote plan to plan_levels_{}.html - best_metric {} cost {} metric as {} battery_value {} keep {} import {} (self {})".format(
                                region_size, best_metric, best_cost, metric, best_battery_value, best_keep, best_import, best_import * self.metric_self_sufficiency
                            )
                        )

                    region_size = int(region_size / 2)

        if debug_mode:
            org_export_limits = self.export_limits_best
            metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
                self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, False, end_record=self.end_record, save="best"
            )
            self.charge_limit_percent_best = calc_percent_limit(self.charge_limit_best, self.soc_max)
            self.update_target_values()
            self.publish_html_plan(self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)
            open("plan_levels.html", "w").write(self.html_plan)
            print("Wrote plan to plan_levels.html best_metric {} metric {}".format(dp2(best_metric), dp2(metric)))

        # Set the new end record and blackout period based on the levelling
        self.end_record = self.record_length(self.charge_window_best, self.charge_limit_best, best_price)
        self.optimise_charge_windows_reset(reset_all=False)
        self.optimise_charge_windows_manual()
        record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
        record_export_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.export_window_best), 1)
        window_sorted, window_index, price_set, price_links = self.sort_window_by_price_combined(
            self.charge_window_best[:record_charge_windows], self.export_window_best[:record_export_windows], calculate_import_low_export=self.calculate_import_low_export, calculate_export_high_import=self.calculate_export_high_import
        )

        self.rate_best_cost_threshold_charge = best_price
        self.rate_best_cost_threshold_export = best_price_export

        # Work out the lowest rate we charge at from the first pass
        lowest_price_charge = best_price
        for price_key in price_set:
            links = price_links[price_key]
            for key in links:
                typ = window_index[key]["type"]
                window_n = window_index[key]["id"]
                price = window_index[key]["average"]
                if typ == "c" and (self.charge_limit_best[window_n] > self.reserve and price < lowest_price_charge):
                    lowest_price_charge = price

        # Optimise individual windows in the price band for charge/export
        # First optimise those at or below threshold highest to lowest (to turn down values)
        # then optimise those above the threshold lowest to highest (to turn up values)
        # Do the opposite for export.
        self.log(
            "Starting second optimisation end_record {} best_price {} best_price_export {} lowest_price_charge {} with charge limits {} export limits {}".format(
                self.time_abs_str(self.end_record + self.minutes_now), best_price, best_price_export, lowest_price_charge, self.charge_limit_best, self.export_limits_best
            )
        )

        for pass_type in ["freeze", "normal", "low"]:
            start_at_low = False
            if pass_type in ["low"]:
                price_set.reverse()
                start_at_low = True

            for price_key in price_set:
                links = price_links[price_key].copy()

                # Freeze pass should be done in time order (newest first)
                if pass_type in ["freeze"]:
                    links.reverse()

                printed_set = False

                for key in links:
                    typ = window_index[key]["type"]
                    window_n = window_index[key]["id"]
                    price = window_index[key]["average"]

                    if typ == "c":
                        # Store price set with window
                        self.charge_window_best[window_n]["set"] = price
                        window_start = self.charge_window_best[window_n]["start"]

                        # Freeze pass is just export freeze
                        if pass_type in ["freeze"]:
                            continue

                        # For start at high only tune down excess high slots
                        if (not start_at_low) and (price > best_price) and (self.charge_limit_best[window_n] != self.soc_max):
                            if self.debug_enable:
                                self.log("Skip start at high window {} best limit {} price_set {}".format(window_n, self.charge_limit_best[window_n], price))
                            continue

                        if self.calculate_best_charge and (window_start not in self.manual_all_times):
                            if not printed_set:
                                self.log(
                                    "Optimise price set {} pass {} price {} start_at_low {} best_price {} best_metric {} best_cost {} best_cycle {} best_carbon {} best_import {}".format(
                                        price_key,
                                        pass_type,
                                        price,
                                        start_at_low,
                                        best_price,
                                        dp2(best_metric),
                                        dp2(best_cost),
                                        dp2(best_cycle),
                                        dp0(best_carbon),
                                        dp2(best_import),
                                    )
                                )
                                printed_set = True
                            average = self.charge_window_best[window_n]["average"]

                            # Don't optimise a charge window that hits an export window if this is disallowed
                            if not self.calculate_export_oncharge:
                                hit_export = self.hit_charge_window(self.export_window_best, self.charge_window_best[window_n]["start"], self.charge_window_best[window_n]["end"])
                                if hit_export >= 0 and self.export_limits_best[hit_export] < 100:
                                    continue

                            best_soc, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon, best_import = self.optimise_charge_limit(
                                window_n,
                                record_charge_windows,
                                self.charge_limit_best,
                                self.charge_window_best,
                                self.export_window_best,
                                self.export_limits_best,
                                end_record=self.end_record,
                            )
                            if best_soc != self.charge_limit_best[window_n]:
                                self.charge_limit_best[window_n] = best_soc
                                if debug_mode:
                                    self.run_prediction(self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, False, end_record=self.end_record, save="best")
                                    self.charge_limit_percent_best = calc_percent_limit(self.charge_limit_best, self.soc_max)
                                    self.update_target_values()
                                    self.publish_html_plan(self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)
                                    open("plan_main_charge_{}.html".format(window_n), "w").write(self.html_plan)
                                    print("Wrote plan to plan_main_charge_{}.html - metric {} cost {} keep {} cycle {} import {}".format(window_n, best_metric, best_cost, best_keep, best_cycle, best_import))

                            if self.debug_enable:
                                self.log(
                                    "Best charge limit window {} time {} - {} cost {} charge_limit {} (adjusted) min {} @ {} (margin added {} and min {} max {}) with metric {} cost {} cycle {} carbon {} import {} windows {}".format(
                                        window_n,
                                        self.time_abs_str(self.charge_window_best[window_n]["start"]),
                                        self.time_abs_str(self.charge_window_best[window_n]["end"]),
                                        average,
                                        dp2(best_soc),
                                        dp2(soc_min),
                                        self.time_abs_str(soc_min_minute),
                                        self.best_soc_margin,
                                        self.best_soc_min,
                                        self.best_soc_max,
                                        dp2(best_metric),
                                        dp2(best_cost),
                                        dp2(best_cycle),
                                        dp0(best_carbon),
                                        dp2(best_import),
                                        calc_percent_limit(self.charge_limit_best, self.soc_max),
                                    )
                                )
                    else:
                        # Store price set with window
                        self.export_window_best[window_n]["set"] = price
                        window_start = self.export_window_best[window_n]["start"]

                        # Ignore freeze pass if export freeze disabled
                        if not self.set_export_freeze and pass_type == "freeze":
                            continue

                        # Do highest price first
                        # Second pass to tune down any excess exports only
                        if pass_type == "low" and ((price > best_price) or (self.export_limits_best[window_n] == 100.0)):
                            continue

                        if self.calculate_best_export and (window_start not in self.manual_all_times):
                            if not self.calculate_export_oncharge:
                                hit_charge = self.hit_charge_window(self.charge_window_best, self.export_window_best[window_n]["start"], self.export_window_best[window_n]["end"])
                                if hit_charge >= 0 and self.charge_limit_best[hit_charge] > 0.0:
                                    continue
                            if not self.car_charging_from_battery and self.hit_car_window(self.export_window_best[window_n]["start"], self.export_window_best[window_n]["end"]):
                                continue
                            if not self.iboost_on_export and self.iboost_enable and self.iboost_plan and (self.hit_charge_window(self.iboost_plan, self.export_window_best[window_n]["start"], self.export_window_best[window_n]["end"]) >= 0):
                                continue

                            average = self.export_window_best[window_n]["average"]
                            if price < lowest_price_charge:
                                if self.debug_enable:
                                    self.log("Skipping export optimisation on rate {} as it is unlikely to be profitable (threshold {} real rate {})".format(price, best_price, dp2(average)))
                                continue

                            if not printed_set:
                                self.log(
                                    "Optimise price set {} pass {} price {} start_at_low {} best_price {} best_metric {} best_cost {} best_cycle {} best_carbon {} best_import {}".format(
                                        price_key,
                                        pass_type,
                                        price,
                                        start_at_low,
                                        best_price,
                                        dp2(best_metric),
                                        dp2(best_cost),
                                        dp2(best_cycle),
                                        dp0(best_carbon),
                                        dp2(best_import),
                                    )
                                )
                                printed_set = True

                            best_soc, best_start, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon, best_import = self.optimise_export(
                                window_n,
                                record_export_windows,
                                self.charge_limit_best,
                                self.charge_window_best,
                                self.export_window_best,
                                self.export_limits_best,
                                end_record=self.end_record,
                                freeze_only=pass_type in ["freeze"],
                            )
                            if best_soc != self.export_limits_best[window_n] or best_start != self.export_window_best[window_n]["start"]:
                                self.export_limits_best[window_n] = best_soc
                                self.export_window_best[window_n]["start"] = best_start
                                if debug_mode:
                                    self.run_prediction(self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, False, end_record=self.end_record, save="best")
                                    self.charge_limit_percent_best = calc_percent_limit(self.charge_limit_best, self.soc_max)
                                    self.update_target_values()
                                    self.publish_html_plan(self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)
                                    open("plan_main_export_{}.html".format(window_n), "w").write(self.html_plan)
                                    print("Wrote plan to plan_main_export_{}.html - metric {} cost {} keep {} cycle {} import {}".format(window_n, best_metric, best_cost, best_keep, best_cycle, best_import))

                            if self.debug_enable:
                                self.log(
                                    "Best export limit window {} time {} - {} cost {} export_limit {} (adjusted) min {} @ {} (margin added {} and min {}) with metric {} cost {} cycle {} carbon {} import {}".format(
                                        window_n,
                                        self.time_abs_str(self.export_window_best[window_n]["start"]),
                                        self.time_abs_str(self.export_window_best[window_n]["end"]),
                                        average,
                                        best_soc,
                                        dp2(soc_min),
                                        self.time_abs_str(soc_min_minute),
                                        self.best_soc_margin,
                                        self.best_soc_min,
                                        dp2(best_metric),
                                        dp2(best_cost),
                                        dp2(best_cycle),
                                        dp0(best_carbon),
                                        dp2(best_import),
                                    )
                                )

            # Log set of charge and export windows
            if self.calculate_best_charge:
                self.log(
                    "Best charge windows best_metric {} best_cost {} best_carbon {} best_import {} metric_keep {} end_record {} windows {}".format(
                        dp2(best_metric),
                        dp2(best_cost),
                        dp0(best_carbon),
                        dp2(best_import),
                        dp2(best_keep),
                        self.time_abs_str(self.end_record + self.minutes_now),
                        self.window_as_text(self.charge_window_best, calc_percent_limit(self.charge_limit_best, self.soc_max), ignore_min=True),
                    )
                )

            if self.calculate_best_export:
                self.log(
                    "Best export windows best_metric {} best_cost {} best_carbon {} best_import {} metric_keep {} end_record {} windows {}".format(
                        dp2(best_metric),
                        dp2(best_cost),
                        dp0(best_carbon),
                        dp2(best_import),
                        dp2(best_keep),
                        self.time_abs_str(self.end_record + self.minutes_now),
                        self.window_as_text(self.export_window_best, self.export_limits_best, ignore_max=True),
                    )
                )

        record_charge_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.charge_window_best), 1)
        record_export_windows = max(self.max_charge_windows(self.end_record + self.minutes_now, self.export_window_best), 1)

        if self.calculate_second_pass:
            self.log("Second pass optimisation started")
            count = 0
            window_sorted, window_index = self.sort_window_by_time_combined(self.charge_window_best[:record_charge_windows], self.export_window_best[:record_export_windows])
            for key in window_sorted:
                typ = window_index[key]["type"]
                window_n = window_index[key]["id"]
                if typ == "c":
                    window_start = self.charge_window_best[window_n]["start"]
                    if self.calculate_best_charge and (window_start not in self.manual_all_times):
                        # Don't optimise a charge window that hits an export window if this is disallowed
                        if not self.calculate_export_oncharge:
                            hit_export = self.hit_charge_window(self.export_window_best, self.charge_window_best[window_n]["start"], self.charge_window_best[window_n]["end"])
                            if hit_export >= 0 and self.export_limits_best[hit_export] < 100:
                                continue

                        best_soc, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon, best_import = self.optimise_charge_limit(
                            window_n,
                            record_charge_windows,
                            self.charge_limit_best,
                            self.charge_window_best,
                            self.export_window_best,
                            self.export_limits_best,
                            end_record=self.end_record,
                        )
                        self.charge_limit_best[window_n] = best_soc
                else:
                    window_start = self.export_window_best[window_n]["start"]
                    if self.calculate_best_export and (window_start not in self.manual_all_times):
                        if not self.calculate_export_oncharge:
                            hit_charge = self.hit_charge_window(self.charge_window_best, self.export_window_best[window_n]["start"], self.export_window_best[window_n]["end"])
                            if hit_charge >= 0 and self.charge_limit_best[hit_charge] > 0.0:
                                continue
                        if not self.car_charging_from_battery and self.hit_car_window(self.export_window_best[window_n]["start"], self.export_window_best[window_n]["end"]):
                            continue
                        if not self.iboost_on_export and self.iboost_enable and self.iboost_plan and (self.hit_charge_window(self.iboost_plan, self.export_window_best[window_n]["start"], self.export_window_best[window_n]["end"]) >= 0):
                            continue

                        average = self.export_window_best[window_n]["average"]
                        best_soc, best_start, best_metric, best_cost, soc_min, soc_min_minute, best_keep, best_cycle, best_carbon, best_import = self.optimise_export(
                            window_n,
                            record_export_windows,
                            self.charge_limit_best,
                            self.charge_window_best,
                            self.export_window_best,
                            self.export_limits_best,
                            end_record=self.end_record,
                        )
                        self.export_limits_best[window_n] = best_soc
                        self.export_window_best[window_n]["start"] = best_start
                if (count % 16) == 0:
                    self.log("Final optimisation type {} window {} metric {} metric_keep {} best_carbon {} best_import {} cost {}".format(typ, window_n, best_metric, dp2(best_keep), dp0(best_carbon), dp2(best_import), dp2(best_cost)))
                count += 1
            self.log("Second pass optimisation finished metric {} cost {} metric_keep {} cycle {} carbon {} import {}".format(best_metric, dp2(best_cost), dp2(best_keep), dp2(best_cycle), dp0(best_carbon), dp2(best_carbon)))

        if debug_mode:
            metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = self.run_prediction(
                self.charge_limit_best, self.charge_window_best, self.export_window_best, self.export_limits_best, False, end_record=self.end_record, save="best"
            )
            self.charge_limit_percent_best = calc_percent_limit(self.charge_limit_best, self.soc_max)
            self.update_target_values()
            self.publish_html_plan(self.pv_forecast_minute_step, self.pv_forecast_minute10_step, self.load_minutes_step, self.load_minutes_step10, self.end_record)
            open("plan_raw.html", "w").write(self.html_plan)
            print("Wrote plan to plan_raw.html")

        return best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import

    def optimise_charge_windows_manual(self):
        """
        Manual window overrides
        """
        if self.charge_window_best and self.calculate_best_charge:
            for window_n in range(len(self.charge_window_best)):
                if self.charge_window_best[window_n]["start"] in self.manual_demand_times:
                    self.charge_limit_best[window_n] = 0
                elif self.charge_window_best[window_n]["start"] in self.manual_export_times:
                    self.charge_limit_best[window_n] = 0
                elif self.charge_window_best[window_n]["start"] in self.manual_freeze_export_times:
                    self.charge_limit_best[window_n] = 0
                elif self.charge_window_best[window_n]["start"] in self.manual_charge_times:
                    self.charge_limit_best[window_n] = self.soc_max
                elif self.charge_window_best[window_n]["start"] in self.manual_freeze_charge_times:
                    self.charge_limit_best[window_n] = self.reserve

        if self.export_window_best and self.calculate_best_export:
            for window_n in range(len(self.export_window_best)):
                if self.export_window_best[window_n]["start"] in self.manual_demand_times:
                    self.export_limits_best[window_n] = 100
                elif self.export_window_best[window_n]["start"] in self.manual_export_times:
                    self.export_limits_best[window_n] = 0
                elif self.export_window_best[window_n]["start"] in self.manual_freeze_export_times:
                    self.export_limits_best[window_n] = 99

    def optimise_charge_windows_reset(self, reset_all):
        """
        Reset the charge windows to min

        Parameters:
        - reset_all (bool): If True, reset all charge windows. If False, reset only the charge windows that are in the record window.

        Returns:
        None
        """
        if self.charge_window_best and self.calculate_best_charge:
            # Set all to max
            for window_n in range(len(self.charge_window_best)):
                if self.charge_window_best[window_n]["start"] < (self.minutes_now + self.end_record):
                    if reset_all:
                        self.charge_limit_best[window_n] = 0.0
                else:
                    self.charge_limit_best[window_n] = self.soc_max

        if self.export_window_best and self.calculate_best_export:
            # Set all to max
            for window_n in range(len(self.export_window_best)):
                if self.export_window_best[window_n]["start"] < (self.minutes_now + self.end_record):
                    if reset_all:
                        self.export_limits_best[window_n] = 100.0
                else:
                    self.export_limits_best[window_n] = 100.0

    def run_prediction(self, charge_limit, charge_window, export_window, export_limits, pv10, end_record, save=None, step=PREDICT_STEP):
        """
        Run a prediction scenario given a charge limit, options to save the results or not to HA entity
        """

        # Call the prediction model
        pred = self.prediction
        (
            final_metric,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            final_soc,
            soc_min_minute,
            final_battery_cycle,
            final_metric_keep,
            final_iboost_kwh,
            final_carbon_g,
        ) = pred.run_prediction(charge_limit, charge_window, export_window, export_limits, pv10, end_record, save, step)
        self.predict_soc = pred.predict_soc
        self.car_charging_soc_next = pred.car_charging_soc_next
        self.iboost_next = pred.iboost_next
        self.iboost_running = pred.iboost_running
        self.iboost_running_solar = pred.iboost_running_solar
        self.iboost_running_full = pred.iboost_running_full
        if save or self.debug_enable:
            predict_soc_time = pred.predict_soc_time
            first_charge = pred.first_charge
            first_charge_soc = pred.first_charge_soc
            predict_car_soc_time = pred.predict_car_soc_time
            predict_battery_power = pred.predict_battery_power
            predict_state = pred.predict_state
            predict_battery_cycle = pred.predict_battery_cycle
            predict_pv_power = pred.predict_pv_power
            predict_grid_power = pred.predict_grid_power
            predict_load_power = pred.predict_load_power
            final_export_kwh = pred.final_export_kwh
            export_kwh_h0 = pred.export_kwh_h0
            final_load_kwh = pred.final_load_kwh
            load_kwh_h0 = pred.load_kwh_h0
            metric_time = pred.metric_time
            record_time = pred.record_time
            predict_iboost = pred.predict_iboost
            predict_carbon_g = pred.predict_carbon_g
            load_kwh_time = pred.load_kwh_time
            pv_kwh_time = pred.pv_kwh_time
            import_kwh_time = pred.import_kwh_time
            export_kwh_time = pred.export_kwh_time
            final_pv_kwh = pred.final_pv_kwh
            export_to_first_charge = pred.export_to_first_charge
            pv_kwh_h0 = pred.pv_kwh_h0
            final_import_kwh = pred.final_import_kwh
            final_import_kwh_house = pred.final_import_kwh_house
            final_import_kwh_battery = pred.final_import_kwh_battery
            hours_left = pred.hours_left
            final_car_soc = pred.final_car_soc
            import_kwh_h0 = pred.import_kwh_h0
            predict_export = pred.predict_export

            if save == "best" or save == "compare":
                self.predict_soc_best = pred.predict_soc_best
                self.predict_iboost_best = pred.predict_iboost_best
                self.predict_metric_best = pred.predict_metric_best
                self.predict_carbon_best = pred.predict_carbon_best
                self.predict_clipped_best = pred.predict_clipped_best

            if self.debug_enable or save:
                self.log(
                    "predict {} end_record {} final soc {} kWh metric {} {} metric_keep {} min_soc {} @ {} kWh load {} pv {}".format(
                        save,
                        self.time_abs_str(end_record + self.minutes_now),
                        round(final_soc, 2),
                        round(final_metric, 2),
                        self.currency_symbols[1],
                        round(final_metric_keep, 2),
                        round(soc_min, 2),
                        self.time_abs_str(soc_min_minute),
                        round(final_load_kwh, 2),
                        round(final_pv_kwh, 2),
                    )
                )
                self.log("         [{}]".format(self.scenario_summary_title(record_time)))
                self.log("    SOC: [{}]".format(self.scenario_summary(record_time, predict_soc_time)))
                self.log("    BAT: [{}]".format(self.scenario_summary(record_time, predict_state)))
                self.log("   LOAD: [{}]".format(self.scenario_summary(record_time, load_kwh_time)))
                self.log("     PV: [{}]".format(self.scenario_summary(record_time, pv_kwh_time)))
                self.log(" IMPORT: [{}]".format(self.scenario_summary(record_time, import_kwh_time)))
                self.log(" EXPORT: [{}]".format(self.scenario_summary(record_time, export_kwh_time)))
                if self.iboost_enable:
                    self.log(" IBOOST: [{}]".format(self.scenario_summary(record_time, predict_iboost)))
                if self.carbon_enable:
                    self.log(" CARBON: [{}]".format(self.scenario_summary(record_time, predict_carbon_g)))
                for car_n in range(self.num_cars):
                    self.log("   CAR{}: [{}]".format(car_n, self.scenario_summary(record_time, predict_car_soc_time[car_n])))
                self.log(" METRIC: [{}]".format(self.scenario_summary(record_time, metric_time)))
                if save == "best":
                    self.log(" STATE:  [{}]".format(self.scenario_summary_state(record_time)))

            # Save data to HA state
            if save and save == "base":
                self.dashboard_item(
                    self.prefix + ".battery_hours_left",
                    state=dp2(hours_left),
                    attributes={"friendly_name": "Predicted Battery Hours left", "state_class": "measurement", "unit_of_measurement": "hours", "icon": "mdi:timelapse"},
                )
                postfix = ""
                for car_n in range(self.num_cars):
                    if car_n > 0:
                        postfix = "_" + str(car_n)
                    self.dashboard_item(
                        self.prefix + ".car_soc" + postfix,
                        state=dp2(final_car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0),
                        attributes={
                            "results": self.filtered_times(predict_car_soc_time[car_n]),
                            "today": self.filtered_today(predict_car_soc_time[car_n]),
                            "friendly_name": "Car " + str(car_n) + " Battery SoC",
                            "state_class": "measurement",
                            "unit_of_measurement": "%",
                            "icon": "mdi:battery",
                        },
                    )
                self.dashboard_item(
                    self.prefix + ".soc_kw_h0",
                    state=dp3(self.predict_soc[0]),
                    attributes={"friendly_name": "Current SoC kWh", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:battery"},
                )
                self.dashboard_item(
                    self.prefix + ".soc_kw",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_soc_time),
                        "today": self.filtered_today(predict_soc_time),
                        "friendly_name": "Predicted SoC kWh",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "first_charge_kwh": first_charge_soc,
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".battery_power",
                    state=dp3(0),
                    attributes={
                        "results": self.filtered_times(predict_battery_power),
                        "today": self.filtered_today(predict_battery_power),
                        "friendly_name": "Predicted Battery Power",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".battery_cycle",
                    state=dp3(final_battery_cycle),
                    attributes={
                        "results": self.filtered_times(predict_battery_cycle),
                        "today": self.filtered_today(predict_battery_cycle),
                        "friendly_name": "Predicted Battery Cycle",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".pv_power",
                    state=dp3(self.pv_power / 1000.0),
                    attributes={
                        "results": self.filtered_times(predict_pv_power),
                        "today": self.filtered_today(predict_pv_power),
                        "friendly_name": "Predicted PV Power",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".grid_power",
                    state=dp3(0),
                    attributes={
                        "results": self.filtered_times(predict_grid_power),
                        "today": self.filtered_today(predict_grid_power),
                        "friendly_name": "Predicted Grid Power",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".load_power",
                    state=dp3(self.load_power / 1000.0),
                    attributes={
                        "results": self.filtered_times(predict_load_power),
                        "today": self.filtered_today(predict_load_power),
                        "friendly_name": "Predicted Load Power",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".soc_min_kwh",
                    state=dp3(soc_min),
                    attributes={
                        "time": self.time_abs_str(soc_min_minute),
                        "friendly_name": "Predicted minimum SoC",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery-arrow-down-outline",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".export_energy",
                    state=dp3(final_export_kwh),
                    attributes={
                        "results": self.filtered_times(export_kwh_time),
                        "today": self.filtered_today(export_kwh_time),
                        "export_until_charge_kwh": dp2(export_to_first_charge),
                        "friendly_name": "Predicted exports",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-export",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".export_energy_h0",
                    state=dp3(export_kwh_h0),
                    attributes={"friendly_name": "Current export kWh", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:transmission-tower-export"},
                )
                self.dashboard_item(
                    self.prefix + ".load_energy",
                    state=dp3(final_load_kwh),
                    attributes={
                        "results": self.filtered_times(load_kwh_time),
                        "today": self.filtered_today(load_kwh_time),
                        "friendly_name": "Predicted load",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:home-lightning-bolt",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".load_energy_h0",
                    state=dp3(load_kwh_h0),
                    attributes={"friendly_name": "Current load kWh", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:home-lightning-bolt"},
                )
                self.dashboard_item(
                    self.prefix + ".pv_energy",
                    state=dp3(final_pv_kwh),
                    attributes={"results": pv_kwh_time, "friendly_name": "Predicted PV", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:solar-power"},
                )
                self.dashboard_item(
                    self.prefix + ".pv_energy_h0",
                    state=dp3(pv_kwh_h0),
                    attributes={"friendly_name": "Current PV kWh", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:solar-power"},
                )
                self.dashboard_item(
                    self.prefix + ".import_energy",
                    state=dp3(final_import_kwh),
                    attributes={
                        "results": self.filtered_times(import_kwh_time),
                        "today": self.filtered_today(import_kwh_time),
                        "friendly_name": "Predicted imports",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".import_energy_h0",
                    state=dp3(import_kwh_h0),
                    attributes={"friendly_name": "Current import kWh", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:transmission-tower-import"},
                )
                self.dashboard_item(
                    self.prefix + ".import_energy_battery",
                    state=dp3(final_import_kwh_battery),
                    attributes={
                        "friendly_name": "Predicted import to battery",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".import_energy_house",
                    state=dp3(final_import_kwh_house),
                    attributes={"friendly_name": "Predicted import to house", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:transmission-tower-import"},
                )
                self.log("Battery has {} hours left - now at {}".format(dp2(hours_left), dp2(self.soc_kw)))
                self.dashboard_item(
                    self.prefix + ".metric",
                    state=dp2(final_metric),
                    attributes={
                        "results": self.filtered_times(metric_time),
                        "today": self.filtered_today(metric_time),
                        "friendly_name": "Predicted metric (cost)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".duration",
                    state=dp2(end_record / 60),
                    attributes={"friendly_name": "Prediction duration", "state_class": "measurement", "unit_of_measurement": "hours", "icon": "mdi:arrow-split-vertical"},
                )
                if self.carbon_enable:
                    self.dashboard_item(
                        self.prefix + ".carbon",
                        state=dp2(final_carbon_g),
                        attributes={
                            "results": self.filtered_times(predict_carbon_g),
                            "today": self.filtered_today(predict_carbon_g),
                            "friendly_name": "Predicted Carbon energy",
                            "state_class": "measurement",
                            "unit_of_measurement": "g",
                            "icon": "mdi:molecule-co2",
                        },
                    )
                    self.dashboard_item(
                        self.prefix + ".carbon_now",
                        state=dp2(self.carbon_intensity.get(0, 0)),
                        attributes={
                            "friendly_name": "Grid carbon intensity now",
                            "state_class": "measurement",
                            "unit_of_measurement": "g/kWh",
                            "icon": "mdi:molecule-co2",
                        },
                    )

            if save and save == "best":
                self.dashboard_item(
                    self.prefix + ".best_battery_hours_left",
                    state=dp2(hours_left),
                    attributes={"friendly_name": "Predicted Battery Hours left best", "state_class": "measurement", "unit_of_measurement": "hours", "icon": "mdi:timelapse"},
                )
                postfix = ""
                for car_n in range(self.num_cars):
                    if car_n > 0:
                        postfix = "_" + str(car_n)
                    self.dashboard_item(
                        self.prefix + ".car_soc_best" + postfix,
                        state=dp2(final_car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0),
                        attributes={
                            "results": self.filtered_times(predict_car_soc_time[car_n]),
                            "today": self.filtered_today(predict_car_soc_time[car_n]),
                            "friendly_name": "Car " + str(car_n) + " Battery SoC best",
                            "state_class": "measurement",
                            "unit_of_measurement": "%",
                            "icon": "mdi:battery",
                        },
                    )

                # Compute battery value now and at end of plan
                rate_min_now = self.rate_min_forward.get(self.minutes_now, self.rate_min) / self.inverter_loss / self.battery_loss + self.metric_battery_cycle
                rate_min_now = max(min(rate_min_now, self.rate_max * self.inverter_loss * self.battery_loss - self.metric_battery_cycle), 0)
                rate_min_end = self.rate_min_forward.get(self.minutes_now + end_record, self.rate_min) / self.inverter_loss / self.battery_loss + self.metric_battery_cycle
                rate_min_end = max(min(rate_min_end, self.rate_max * self.inverter_loss * self.battery_loss - self.metric_battery_cycle), 0)
                rate_export_min_now = self.rate_export_min * self.inverter_loss * self.battery_loss_discharge - self.metric_battery_cycle - rate_min_now
                rate_export_min_end = self.rate_export_min * self.inverter_loss * self.battery_loss_discharge - self.metric_battery_cycle - rate_min_end
                value_kwh_now = self.metric_battery_value_scaling * max(rate_min_now, 1.0, rate_export_min_now)
                value_kwh_end = self.metric_battery_value_scaling * max(rate_min_end, 1.0, rate_export_min_end)

                self.dashboard_item(
                    self.prefix + ".soc_kw_best",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_soc_time),
                        "today": self.filtered_today(predict_soc_time),
                        "friendly_name": "Battery SoC kWh best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "first_charge_kwh": first_charge_soc,
                        "soc_now": dp2(self.soc_kw),
                        "value_per_kwh_now": dp2(value_kwh_now),
                        "value_per_kwh_end": dp2(value_kwh_end),
                        "value_now": dp2(self.soc_kw * value_kwh_now),
                        "value_end": dp2(final_soc * value_kwh_end),
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".battery_power_best",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_battery_power),
                        "today": self.filtered_today(predict_battery_power),
                        "friendly_name": "Predicted Battery Power Best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".battery_cycle_best",
                    state=dp3(final_battery_cycle),
                    attributes={
                        "results": self.filtered_times(predict_battery_cycle),
                        "today": self.filtered_today(predict_battery_cycle),
                        "friendly_name": "Predicted Battery Cycle Best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".pv_power_best",
                    state=dp3(0),
                    attributes={
                        "results": self.filtered_times(predict_pv_power),
                        "today": self.filtered_today(predict_pv_power),
                        "friendly_name": "Predicted PV Power Best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".grid_power_best",
                    state=dp3(0),
                    attributes={
                        "results": self.filtered_times(predict_grid_power),
                        "today": self.filtered_today(predict_grid_power),
                        "friendly_name": "Predicted Grid Power Best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".load_power_best",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_load_power),
                        "today": self.filtered_today(predict_load_power),
                        "friendly_name": "Predicted Load Power Best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".soc_kw_best_h1",
                    state=dp3(self.predict_soc[60]),
                    attributes={"friendly_name": "Predicted SoC kWh best + 1h", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:battery"},
                )
                self.dashboard_item(
                    self.prefix + ".soc_kw_best_h8",
                    state=dp3(self.predict_soc[60 * 8]),
                    attributes={"friendly_name": "Predicted SoC kWh best + 8h", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:battery"},
                )
                self.dashboard_item(
                    self.prefix + ".soc_kw_best_h12",
                    state=dp3(self.predict_soc[60 * 12]),
                    attributes={"friendly_name": "Predicted SoC kWh best + 12h", "state_class": "measurement", "unit _of_measurement": "kWh", "icon": "mdi:battery"},
                )
                self.dashboard_item(
                    self.prefix + ".best_soc_min_kwh",
                    state=dp3(soc_min),
                    attributes={
                        "time": self.time_abs_str(soc_min_minute),
                        "friendly_name": "Predicted minimum SoC best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery-arrow-down-outline",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_export_energy",
                    state=dp3(final_export_kwh),
                    attributes={
                        "results": self.filtered_times(export_kwh_time),
                        "today": self.filtered_today(export_kwh_time),
                        "export_until_charge_kwh": dp2(export_to_first_charge),
                        "friendly_name": "Predicted exports best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-export",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_load_energy",
                    state=dp3(final_load_kwh),
                    attributes={
                        "results": self.filtered_times(load_kwh_time),
                        "today": self.filtered_today(load_kwh_time),
                        "friendly_name": "Predicted load best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:home-lightning-bolt",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_pv_energy",
                    state=dp3(final_pv_kwh),
                    attributes={
                        "results": self.filtered_times(pv_kwh_time),
                        "today": self.filtered_today(pv_kwh_time),
                        "friendly_name": "Predicted PV best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:solar-power",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_import_energy",
                    state=dp3(final_import_kwh),
                    attributes={
                        "results": self.filtered_times(import_kwh_time),
                        "today": self.filtered_today(import_kwh_time),
                        "friendly_name": "Predicted imports best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_import_energy_battery",
                    state=dp3(final_import_kwh_battery),
                    attributes={
                        "friendly_name": "Predicted import to battery best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_import_energy_house",
                    state=dp3(final_import_kwh_house),
                    attributes={
                        "friendly_name": "Predicted import to house best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best_metric",
                    state=dp2(final_metric),
                    attributes={
                        "results": self.filtered_times(metric_time),
                        "today": self.filtered_today(metric_time),
                        "friendly_name": "Predicted best metric (cost)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
                self.dashboard_item(self.prefix + ".record", state=0.0, attributes={"results": self.filtered_times(record_time), "friendly_name": "Prediction window", "state_class": "measurement"})
                self.dashboard_item(
                    self.prefix + ".iboost_best",
                    state=dp2(final_iboost_kwh),
                    attributes={
                        "results": self.filtered_times(predict_iboost),
                        "today": self.filtered_today(predict_iboost, resetmidnight=True),
                        "friendly_name": "Predicted iBoost energy best",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:water-boiler",
                    },
                )
                self.dashboard_item(
                    "binary_sensor." + self.prefix + "_iboost_active",
                    state=self.iboost_running,
                    attributes={"friendly_name": "iBoost active", "icon": "mdi:water-boiler", "solar": self.iboost_running_solar, "full": self.iboost_running_full},
                )
                self.find_spare_energy(self.predict_soc, predict_export, step, first_charge)
                if self.carbon_enable:
                    self.dashboard_item(
                        self.prefix + ".carbon_best",
                        state=dp2(final_carbon_g),
                        attributes={
                            "results": self.filtered_times(predict_carbon_g),
                            "today": self.filtered_today(predict_carbon_g),
                            "friendly_name": "Predicted Carbon energy best",
                            "state_class": "measurement",
                            "unit_of_measurement": "g",
                            "icon": "mdi:molecule-co2",
                        },
                    )

            if save and save == "debug":
                self.dashboard_item(
                    self.prefix + ".pv_power_debug",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_pv_power),
                        "today": self.filtered_today(predict_pv_power),
                        "friendly_name": "Predicted PV Power Debug",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".grid_power_debug",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_grid_power),
                        "today": self.filtered_today(predict_grid_power),
                        "friendly_name": "Predicted Grid Power Debug",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".load_power_debug",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_load_power),
                        "today": self.filtered_today(predict_load_power),
                        "friendly_name": "Predicted Load Power Debug",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".battery_power_debug",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_battery_power),
                        "today": self.filtered_today(predict_battery_power),
                        "friendly_name": "Predicted Battery Power Debug",
                        "state_class": "measurement",
                        "unit_of_measurement": "kW",
                        "icon": "mdi:battery",
                    },
                )

            if save and save == "best10":
                self.dashboard_item(
                    self.prefix + ".soc_kw_best10",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_soc_time),
                        "today": self.filtered_today(predict_soc_time),
                        "friendly_name": "Battery SoC kWh best 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "first_charge_kwh": first_charge_soc,
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best10_pv_energy",
                    state=dp3(final_pv_kwh),
                    attributes={
                        "results": self.filtered_times(pv_kwh_time),
                        "today": self.filtered_today(pv_kwh_time),
                        "friendly_name": "Predicted PV best 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:solar-power",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best10_metric",
                    state=dp2(final_metric),
                    attributes={
                        "results": self.filtered_times(metric_time),
                        "today": self.filtered_today(metric_time),
                        "friendly_name": "Predicted best 10% metric (cost)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best10_export_energy",
                    state=dp3(final_export_kwh),
                    attributes={
                        "results": self.filtered_times(export_kwh_time),
                        "today": self.filtered_today(export_kwh_time),
                        "export_until_charge_kwh": dp2(export_to_first_charge),
                        "friendly_name": "Predicted exports best 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-export",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".best10_load_energy",
                    state=dp3(final_load_kwh),
                    attributes={"friendly_name": "Predicted load best 10%", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:home-lightning-bolt"},
                )
                self.dashboard_item(
                    self.prefix + ".best10_import_energy",
                    state=dp3(final_import_kwh),
                    attributes={
                        "results": self.filtered_times(import_kwh_time),
                        "today": self.filtered_today(import_kwh_time),
                        "friendly_name": "Predicted imports best 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )

            if save and save == "base10":
                self.dashboard_item(
                    self.prefix + ".soc_kw_base10",
                    state=dp3(final_soc),
                    attributes={
                        "results": self.filtered_times(predict_soc_time),
                        "today": self.filtered_today(predict_soc_time),
                        "friendly_name": "Battery SoC kWh base 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:battery",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".base10_pv_energy",
                    state=dp3(final_pv_kwh),
                    attributes={
                        "results": self.filtered_times(pv_kwh_time),
                        "today": self.filtered_today(pv_kwh_time),
                        "friendly_name": "Predicted PV base 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:solar-power",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".base10_metric",
                    state=dp2(final_metric),
                    attributes={
                        "results": self.filtered_times(metric_time),
                        "today": self.filtered_today(metric_time),
                        "friendly_name": "Predicted base 10% metric (cost)",
                        "state_class": "measurement",
                        "unit_of_measurement": self.currency_symbols[1],
                        "icon": "mdi:currency-usd",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".base10_export_energy",
                    state=dp3(final_export_kwh),
                    attributes={
                        "results": self.filtered_times(export_kwh_time),
                        "today": self.filtered_today(export_kwh_time),
                        "export_until_charge_kwh": dp2(export_to_first_charge),
                        "friendly_name": "Predicted exports base 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-export",
                    },
                )
                self.dashboard_item(
                    self.prefix + ".base10_load_energy",
                    state=dp3(final_load_kwh),
                    attributes={"friendly_name": "Predicted load base 10%", "state_class": "measurement", "unit_of_measurement": "kWh", "icon": "mdi:home-lightning-bolt"},
                )
                self.dashboard_item(
                    self.prefix + ".base10_import_energy",
                    state=dp3(final_import_kwh),
                    attributes={
                        "results": self.filtered_times(import_kwh_time),
                        "today": self.filtered_today(import_kwh_time),
                        "friendly_name": "Predicted imports base 10%",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:transmission-tower-import",
                    },
                )

        return (
            final_metric,
            import_kwh_battery,
            import_kwh_house,
            export_kwh,
            soc_min,
            final_soc,
            soc_min_minute,
            final_battery_cycle,
            final_metric_keep,
            final_iboost_kwh,
            final_carbon_g,
        )

    def plan_iboost_smart(self):
        """
        Smart iboost planning
        """
        plan = []
        iboost_today = self.iboost_today
        iboost_max = self.iboost_max_energy
        iboost_power = self.iboost_max_power * 60
        iboost_min_length = max(int((self.iboost_smart_min_length + 29) / 30) * 30, 30)

        self.log("Create iBoost smart plan, max {} kWh, power {} kW, min length {} minutes".format(iboost_max, iboost_power, iboost_min_length))

        low_rates = []
        start_minute = int(self.minutes_now / 30) * 30
        for minute in range(start_minute, start_minute + self.forecast_minutes, 30):
            import_rate = 0
            export_rate = 0
            slot_length = 0
            slot_count = 0
            for slot_start in range(minute, minute + iboost_min_length, 30):
                import_rate += self.rate_import.get(minute, self.rate_min)
                export_rate += self.rate_export.get(minute, 0)
                slot_length += 30
                slot_count += 1
            if slot_count:
                low_rates.append({"start": minute, "end": minute + slot_length, "average": import_rate / slot_count, "export": export_rate / slot_count})

        # Get prices
        if self.iboost_smart:
            price_sorted = self.sort_window_by_price(low_rates, reverse_time=True)
            price_sorted.reverse()
        else:
            price_sorted = [n for n in range(len(low_rates))]

        total_days = int((self.forecast_minutes + self.minutes_now + 24 * 60 - 1) / (24 * 60))
        iboost_soc = [0 for n in range(total_days)]
        iboost_soc[0] = iboost_today

        used_slots = {}
        for window_n in price_sorted:
            window = low_rates[window_n]
            price = window["average"]
            export_price = window["export"]

            length = 0
            kwh = 0

            for day in range(0, total_days):
                day_start_minutes = day * 24 * 60
                day_end_minutes = day_start_minutes + 24 * 60

                slot_start = max(window["start"], self.minutes_now, day_start_minutes)
                slot_end = min(window["end"], day_end_minutes)

                if slot_start < slot_end:
                    rate_okay = True

                    for start in range(slot_start, slot_end, 30):
                        end = min(start + 30, slot_end)

                        # Avoid duplicate slots
                        if minute in used_slots:
                            rate_okay = False

                        # Boost on import/export rate
                        if price > self.iboost_rate_threshold:
                            rate_okay = False
                        if export_price > self.iboost_rate_threshold_export:
                            rate_okay = False

                        # Boost on gas rate vs import price
                        if self.iboost_gas and self.rate_gas:
                            gas_rate = self.rate_gas.get(start, 99) * self.iboost_gas_scale
                            if price > gas_rate:
                                rate_okay = False

                        # Boost on gas rate vs export price
                        if self.iboost_gas_export and self.rate_gas:
                            gas_rate = self.rate_gas.get(start, 99) * self.iboost_gas_scale
                            if export_price > gas_rate:
                                rate_okay = False

                        if not rate_okay:
                            continue

                        # Work out charging amounts
                        length = end - start
                        hours = length / 60
                        kwh = iboost_power * hours
                        kwh = min(kwh, iboost_max - iboost_soc[day])
                        if kwh > 0:
                            iboost_soc[day] = dp3(iboost_soc[day] + kwh)
                            new_slot = {}
                            new_slot["start"] = start
                            new_slot["end"] = end
                            new_slot["kwh"] = dp3(kwh)
                            new_slot["average"] = window["average"]
                            new_slot["cost"] = dp2(new_slot["average"] * kwh)
                            plan.append(new_slot)
                            used_slots[start] = True

        # Return sorted back in time order
        plan = self.sort_window_by_time(plan)
        return plan

    def plan_car_charging(self, car_n, low_rates):
        """
        Plan when the car will charge, taking into account ready time and pricing
        """
        plan = []
        car_soc = self.car_charging_soc[car_n]
        max_price = self.car_charging_plan_max_price[car_n]

        if self.car_charging_plan_smart[car_n]:
            price_sorted = self.sort_window_by_price(low_rates, reverse_time=True)
            price_sorted.reverse()
        else:
            price_sorted = [n for n in range(len(low_rates))]

        try:
            ready_time = datetime.strptime(self.car_charging_plan_time[car_n], "%H:%M:%S")
        except (ValueError, TypeError):
            ready_time = datetime.strptime("07:00:00", "%H:%M:%S")
            self.log("Warn: Car charging plan time for car {} is invalid".format(car_n))

        ready_minutes = ready_time.hour * 60 + ready_time.minute

        # Ready minutes wrap?
        if ready_minutes < self.minutes_now:
            ready_minutes += 24 * 60

        # Car charging now override
        extra_slot = {}
        if self.car_charging_now[car_n]:
            start = int(self.minutes_now / 30) * 30
            end = start + 30
            extra_slot["start"] = start
            extra_slot["end"] = end
            extra_slot["average"] = self.rate_import.get(start, self.rate_min)
            self.log("Car is charging now slot {}".format(extra_slot))

            for window_p in price_sorted:
                window = low_rates[window_p]
                if window["start"] == start:
                    price_sorted.remove(window_p)
                    self.log("Remove old window {}".format(window_p))
                    break

            price_sorted = [-1] + price_sorted

        for window_n in price_sorted:
            if window_n == -1:
                window = extra_slot
            else:
                window = low_rates[window_n]

            start = max(window["start"], self.minutes_now)
            end = min(window["end"], ready_minutes)
            price = window["average"]

            length = 0
            kwh = 0

            # Stop once we have enough charge, allow small margin for rounding
            if (car_soc + 0.1) >= self.car_charging_limit[car_n]:
                break

            # Skip past windows
            if end <= start:
                continue

            # Skip over prices when they are too high
            if (max_price != 0) and price > max_price:
                continue

            # Compute amount of charge
            length = end - start
            hours = length / 60
            kwh = self.car_charging_rate[car_n] * hours

            kwh_add = kwh * self.car_charging_loss
            kwh_left = max(self.car_charging_limit[car_n] - car_soc, 0)

            # Clamp length to required amount (shorten the window)
            if kwh_add > kwh_left:
                percent = kwh_left / kwh_add
                length = min(round(((length * percent) / 5) + 0.5, 0) * 5, end - start)
                end = start + length
                hours = length / 60
                kwh = self.car_charging_rate[car_n] * hours
                kwh_add = min(kwh * self.car_charging_loss, kwh_left)
                kwh = kwh_add / self.car_charging_loss

            # Work out charging amounts
            if kwh > 0:
                car_soc = dp3(car_soc + kwh_add)
                new_slot = {}
                new_slot["start"] = start
                new_slot["end"] = end
                new_slot["kwh"] = dp3(kwh)
                new_slot["average"] = window["average"]
                new_slot["cost"] = dp2(new_slot["average"] * kwh)
                plan.append(new_slot)

        # Return sorted back in time order
        plan = self.sort_window_by_time(plan)
        return plan

    def car_charge_slot_kwh(self, minute_start, minute_end):
        """
        Work out car charging amount in KWh for given 30-minute slot
        """
        car_charging_kwh = 0.0
        if self.num_cars > 0:
            for car_n in range(self.num_cars):
                for window in self.car_charging_slots[car_n]:
                    start = window["start"]
                    end = window["end"]
                    kwh = dp2(window["kwh"]) / (end - start)
                    for minute_offset in range(minute_start, minute_end, PREDICT_STEP):
                        if minute_offset >= start and minute_offset < end:
                            car_charging_kwh += kwh * PREDICT_STEP
            car_charging_kwh = dp2(car_charging_kwh)
        return car_charging_kwh

    def hit_car_window(self, window_start, window_end):
        """
        Does this window intersect a car charging window?
        """
        if self.num_cars > 0:
            for car_n in range(self.num_cars):
                for window in self.car_charging_slots[car_n]:
                    start = window["start"]
                    end = window["end"]
                    kwh = dp2(window["kwh"])
                    if end > window_start and start < window_end and kwh > 0:
                        return True
        return False
