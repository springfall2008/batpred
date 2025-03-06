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
from config import MINUTE_WATT, PREDICT_STEP, TIME_FORMAT, TIME_FORMAT_SECONDS, TIME_FORMAT_OCTOPUS


def time_string_to_stamp(time_string):
    """
    Convert a time string to a timestamp
    """
    if time_string is None:
        return None
    if time_string == "unknown":
        return None

    if isinstance(time_string, str) and len(time_string) == 5:
        time_string += ":00"

    return datetime.strptime(time_string, "%H:%M:%S")


def minutes_since_yesterday(now):
    """
    Calculate the number of minutes since 23:59 yesterday
    """
    yesterday = now - timedelta(days=1)
    yesterday_at_2359 = datetime.combine(yesterday, datetime.max.time())
    difference = now - yesterday_at_2359
    difference_minutes = int((difference.seconds + 59) / 60)
    return difference_minutes


def dp0(value):
    """
    Round to 0 decimal places
    """
    return round(value)


def dp1(value):
    """
    Round to 1 decimal place
    """
    return round(value, 1)


def dp2(value):
    """
    Round to 2 decimal places
    """
    return round(value, 2)


def dp3(value):
    """
    Round to 3 decimal places
    """
    return round(value, 3)


def dp4(value):
    """
    Round to 4 decimal places
    """
    return round(value, 4)


def minutes_to_time(updated, now):
    """
    Compute the number of minutes between a time (now) and the updated time
    """
    timeday = updated - now
    minutes = int(timeday.seconds / 60) + int(timeday.days * 60 * 24)
    return minutes


def str2time(str):
    if "." in str:
        tdata = datetime.strptime(str, TIME_FORMAT_SECONDS)
    elif "T" in str:
        tdata = datetime.strptime(str, TIME_FORMAT)
    else:
        tdata = datetime.strptime(str, TIME_FORMAT_OCTOPUS)
    return tdata


def calc_percent_limit(charge_limit, soc_max):
    """
    Calculate a charge limit in percent
    """
    if isinstance(charge_limit, list):
        if soc_max <= 0:
            return [0 for i in range(len(charge_limit))]
        else:
            return [min(int((float(charge_limit[i]) / soc_max * 100.0) + 0.5), 100) for i in range(len(charge_limit))]
    else:
        if soc_max <= 0:
            return 0
        else:
            return min(int((float(charge_limit) / soc_max * 100.0) + 0.5), 100)


def remove_intersecting_windows(charge_limit_best, charge_window_best, export_limit_best, export_window_best):
    """
    Filters and removes intersecting charge windows
    """
    clip_again = True

    # For each charge window
    while clip_again:
        clip_again = False
        new_limit_best = []
        new_window_best = []
        for window_n in range(len(charge_limit_best)):
            window = charge_window_best[window_n]
            start = window["start"]
            end = window["end"]
            average = window["average"]
            limit = charge_limit_best[window_n]
            clipped = False

            # For each discharge window
            for dwindow_n in range(len(export_limit_best)):
                dwindow = export_window_best[dwindow_n]
                dlimit = export_limit_best[dwindow_n]
                dstart = dwindow["start"]
                dend = dwindow["end"]

                # Overlapping window with enabled discharge?
                if (limit > 0.0) and (dlimit < 100.0) and (dstart < end) and (dend >= start):
                    if dstart <= start:
                        if start != dend:
                            start = dend
                            clipped = True
                    elif dend >= end:
                        if end != dstart:
                            end = dstart
                            clipped = True
                    else:
                        # Two segments
                        if (dstart - start) >= 5:
                            new_window = {}
                            new_window["start"] = start
                            new_window["end"] = dstart
                            new_window["average"] = average
                            new_window_best.append(new_window)
                            new_limit_best.append(limit)
                        start = dend
                        clipped = True
                        if (end - start) >= 5:
                            clip_again = True

            if not clipped or ((end - start) >= 5):
                new_window = {}
                new_window["start"] = start
                new_window["end"] = end
                new_window["average"] = average
                new_window_best.append(new_window)
                new_limit_best.append(limit)

        if clip_again:
            charge_window_best = new_window_best.copy()
            charge_limit_best = new_limit_best.copy()

    return new_limit_best, new_window_best


def get_charge_rate_curve(soc, charge_rate_setting, soc_max, battery_rate_max_charge, battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve, debug=False):
    """
    Compute true charging rate from SOC and charge rate setting
    """
    soc_percent = calc_percent_limit(soc, soc_max)
    max_charge_rate = battery_rate_max_charge * battery_charge_power_curve.get(soc_percent, 1.0)

    # Temperature cap
    max_rate_cap = find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, battery_rate_max_charge)
    max_charge_rate = min(max_charge_rate, max_rate_cap)

    if debug:
        print(
            "Max charge rate: {} SOC: {} Percent {} Rate in: {} rate out: {} cap: {}".format(
                max_charge_rate * MINUTE_WATT, soc, soc_percent, charge_rate_setting * MINUTE_WATT, min(charge_rate_setting, max_charge_rate) * MINUTE_WATT, max_rate_cap * MINUTE_WATT
            )
        )
    return max(min(charge_rate_setting, max_charge_rate), battery_rate_min)


def get_discharge_rate_curve(soc, discharge_rate_setting, soc_max, battery_rate_max_discharge, battery_discharge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve):
    """
    Compute true discharging rate from SOC and charge rate setting
    """
    soc_percent = calc_percent_limit(soc, soc_max)
    max_discharge_rate = battery_rate_max_discharge * battery_discharge_power_curve.get(soc_percent, 1.0)
    max_rate_cap = find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, battery_rate_max_discharge)
    max_discharge_rate = min(max_discharge_rate, max_rate_cap)

    return max(min(discharge_rate_setting, max_discharge_rate), battery_rate_min)


def find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, max_rate):
    """
    Find the battery temperature cap
    """
    battery_temperature_idx = min(battery_temperature, 20)
    battery_temperature_idx = max(battery_temperature_idx, -20)
    if battery_temperature_idx in battery_temperature_curve:
        battery_temperature_adjust = battery_temperature_curve[battery_temperature_idx]
    elif battery_temperature_idx > 0:
        battery_temperature_adjust = battery_temperature_curve.get(20, 1.0)
    else:
        battery_temperature_adjust = battery_temperature_curve.get(0, 1.0)
    battery_temperature_rate_cap = soc_max * battery_temperature_adjust / 60.0

    return min(battery_temperature_rate_cap, max_rate)


def find_charge_rate(
    minutes_now,
    soc,
    window,
    target_soc,
    max_rate,
    soc_max,
    battery_charge_power_curve,
    set_charge_low_power,
    charge_low_power_margin,
    battery_rate_min,
    battery_rate_max_scaling,
    battery_loss,
    log_to,
    battery_temperature=20,
    battery_temperature_curve={},
    current_charge_rate=None,
):
    """
    Find the lowest charge rate that fits the charge slow
    """
    margin = charge_low_power_margin
    target_soc = round(target_soc, 2)

    # Current charge rate
    if current_charge_rate is None:
        current_charge_rate = max_rate

    # Real achieved max rate
    max_rate_real = get_charge_rate_curve(soc, max_rate, soc_max, max_rate, battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve) * battery_rate_max_scaling

    if set_charge_low_power:
        minutes_left = window["end"] - minutes_now - margin
        abs_minutes_left = window["end"] - minutes_now

        # If we don't have enough minutes left go to max
        if abs_minutes_left < 0:
            if log_to:
                log_to("Low power mode: abs_minutes_left {} < 0, default to max rate".format(abs_minutes_left))
            return max_rate, max_rate_real

        # If we already have reached target go back to max
        if round(soc, 2) >= target_soc:
            if log_to:
                log_to("Low power mode: soc {} >= target_soc {}, default to max rate".format(soc, target_soc))
            return max_rate, max_rate_real

        # Work out the charge left in kw
        charge_left = round(target_soc - soc, 2)

        # If we can never hit the target then go to max
        if round(max_rate_real * abs_minutes_left, 2) <= charge_left:
            if log_to:
                log_to(
                    "Low power mode: Can't hit target: max_rate * abs_minutes_left = {} <= charge_left {}, minutes_left {} window_end {} minutes_now {} default to max rate".format(
                        max_rate_real * abs_minutes_left, charge_left, abs_minutes_left, window["end"], minutes_now
                    )
                )
            return max_rate, max_rate_real

        # What's the lowest we could go?
        min_rate = charge_left / abs_minutes_left
        min_rate_w = int(min_rate * MINUTE_WATT)

        # Apply the curve at each rate to pick one that works
        rate_w = max_rate * MINUTE_WATT
        best_rate = max_rate
        best_rate_real = max_rate_real
        highest_achievable_rate = 0

        if log_to:
            log_to(
                "Find charge rate for low power mode: soc: {} target_soc: {} charge_left: {} minutes_left: {} abs_minutes_left: {} max_rate: {} min_rate: {} min_rate_w: {}".format(
                    soc, target_soc, charge_left, minutes_left, abs_minutes_left, max_rate * MINUTE_WATT, min_rate * MINUTE_WATT, min_rate_w
                )
            )

        while rate_w >= 400:
            rate = rate_w / MINUTE_WATT
            if rate_w >= min_rate_w:
                charge_now = soc
                minute = 0
                rate_scale_max = 0
                # Compute over the time period, include the completion time
                for minute in range(0, minutes_left, PREDICT_STEP):
                    rate_scale = get_charge_rate_curve(charge_now, rate, soc_max, max_rate, battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve)
                    highest_achievable_rate = max(highest_achievable_rate, rate_scale)
                    rate_scale *= battery_rate_max_scaling
                    rate_scale_max = max(rate_scale_max, rate_scale)
                    charge_amount = rate_scale * PREDICT_STEP * battery_loss
                    charge_now += charge_amount
                    if (round(charge_now, 2) >= target_soc) and (rate_scale_max < best_rate_real):
                        best_rate = rate
                        best_rate_real = rate_scale_max
                        break
                # if log_to:
                #   log_to("Low Power mode: rate: {} minutes: {} SOC: {} Target SOC: {} Charge left: {} Charge now: {} Rate scale: {} Charge amount: {} Charge now: {} best rate: {} highest achievable_rate {}".format(
                #        rate * MINUTE_WATT, minute, soc, target_soc, charge_left, charge_now, rate_scale * MINUTE_WATT, charge_amount, round(charge_now, 2), best_rate*MINUTE_WATT, highest_achievable_rate*MINUTE_WATT))
            else:
                break
            rate_w -= 100.0

        # Stick with current rate if it doesn't matter
        if best_rate >= highest_achievable_rate and current_charge_rate >= highest_achievable_rate:
            best_rate = current_charge_rate
            if log_to:
                log_to("Low Power mode: best rate {} is greater than highest achievable rate {} and current rate {} so sticking with current rate".format(best_rate * MINUTE_WATT, highest_achievable_rate * MINUTE_WATT, current_charge_rate * MINUTE_WATT))

        best_rate_real = get_charge_rate_curve(soc, best_rate, soc_max, max_rate, battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve) * battery_rate_max_scaling
        if log_to:
            log_to(
                "Low Power mode: minutes left: {} absolute: {} SOC: {} Target SOC: {} Charge left: {} Max rate: {} Min rate: {} Best rate: {} Best rate real: {} Battery temp {}".format(
                    minutes_left, abs_minutes_left, soc, target_soc, charge_left, max_rate * MINUTE_WATT, min_rate * MINUTE_WATT, best_rate * MINUTE_WATT, best_rate_real * MINUTE_WATT, battery_temperature
                )
            )
        return best_rate, best_rate_real
    else:
        return max_rate, max_rate_real
