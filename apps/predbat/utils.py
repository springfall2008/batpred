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


def get_charge_rate_curve(model, soc, charge_rate_setting, debug=False):
    """
    Compute true charging rate from SOC and charge rate setting
    """
    soc_percent = calc_percent_limit(soc, model.soc_max)
    max_charge_rate = model.battery_rate_max_charge * model.battery_charge_power_curve.get(soc_percent, 1.0) * model.battery_rate_max_scaling
    if debug:
        print("Max charge rate: {} SOC: {} Charge rate setting: {}".format(max_charge_rate, soc, charge_rate_setting))
    return max(min(charge_rate_setting, max_charge_rate), model.battery_rate_min)


def get_discharge_rate_curve(model, soc, discharge_rate_setting):
    """
    Compute true discharging rate from SOC and charge rate setting
    """
    soc_percent = calc_percent_limit(soc, model.soc_max)
    max_discharge_rate = model.battery_rate_max_discharge * model.battery_discharge_power_curve.get(soc_percent, 1.0) * model.battery_rate_max_scaling_discharge
    return max(min(discharge_rate_setting, max_discharge_rate), model.battery_rate_min)


def find_charge_rate(model, minutes_now, soc, window, target_soc, max_rate, quiet=True):
    """
    Find the lowest charge rate that fits the charge slow
    """
    margin = model.charge_low_power_margin
    target_soc = round(target_soc, 2)

    if model.set_charge_low_power:
        minutes_left = window["end"] - minutes_now - margin

        # If we don't have enough minutes left go to max
        if minutes_left < 0:
            return max_rate

        # If we already have reached target go back to max
        if soc >= target_soc:
            return max_rate

        # Work out the charge left in kw
        charge_left = target_soc - soc

        # If we can never hit the target then go to max
        if max_rate * minutes_left < charge_left:
            return max_rate

        # What's the lowest we could go?
        min_rate = charge_left / minutes_left
        min_rate_w = int(min_rate * MINUTE_WATT)

        # Apply the curve at each rate to pick one that works
        rate_w = max_rate * MINUTE_WATT
        best_rate = max_rate
        while rate_w >= 400:
            rate = rate_w / MINUTE_WATT
            if rate_w >= min_rate_w:
                charge_now = soc
                minute = 0
                # Compute over the time period, include the completion time
                for minute in range(0, minutes_left, PREDICT_STEP):
                    rate_scale = get_charge_rate_curve(model, charge_now, rate)
                    charge_amount = rate_scale * PREDICT_STEP * model.battery_loss
                    charge_now += charge_amount
                    if round(charge_now, 2) >= target_soc:
                        best_rate = rate
                        break
            rate_w -= 100.0
        return best_rate
    else:
        return max_rate
