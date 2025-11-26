# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timedelta, timezone
from config import MINUTE_WATT, PREDICT_STEP, TIME_FORMAT, TIME_FORMAT_SECONDS, TIME_FORMAT_OCTOPUS, MAX_INCREMENT, TIME_FORMAT_DAILY

DAY_OF_WEEK_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def get_now_from_cumulative(data, minutes_now, backwards):
    """
    Get current value from cumulative data
    """
    if backwards:
        # Work out the lowest value in a 5 minute period between minutes_now and minutes_now - 5
        lowest = 9999999999
        for minute in range(0, 5):
            lowest = min(data.get(minutes_now - minute, lowest), lowest)
        value = data.get(0, 0) - lowest
    else:
        lowest = 9999999999
        for minute in range(0, 5):
            lowest = min(data.get(minute, lowest), lowest)
        value = data.get(minutes_now, 0) - lowest
    return max(value, 0)


def prune_today(data, now_utc, midnight_utc, prune=True, group=15, prune_future=False, intermediate=False):
    """
    Remove data from before today
    """
    results = {}
    last_time = None
    prev_value = None
    for key in data:
        # Convert key in format '2024-09-07T15:40:09.799567+00:00' into a datetime
        if "." in key:
            timekey = datetime.strptime(key, TIME_FORMAT_SECONDS)
        else:
            timekey = datetime.strptime(key, TIME_FORMAT)
        if last_time and (timekey - last_time).seconds < group * 60:
            continue
        if intermediate and last_time and ((timekey - last_time).seconds > group * 60):
            # Large gap, introduce intermediate data point
            seconds_gap = int((timekey - last_time).total_seconds())
            for i in range(1, seconds_gap // int(group * 60)):
                new_time = last_time + timedelta(seconds=i * group * 60)
                results[new_time.strftime(TIME_FORMAT)] = prev_value
        if not prune or (timekey > midnight_utc):
            if prune_future and (timekey > now_utc):
                continue
            results[key] = data[key]
            last_time = timekey
            prev_value = data[key]
    return results


def history_attribute(history, state_key="state", last_updated_key="last_updated", scale=1.0, attributes=False, daily=False, offset_days=0, first=True, pounds=False):
    """
    Get historical data for an attribute
    """
    results = {}
    last_updated_time = None
    last_day_stamp = None

    if not isinstance(history, list):
        return results

    if history and len(history) >= 1:
        history = history[0]

    if not isinstance(history, list):
        return results

    # Process history
    for item in history:
        if last_updated_key not in item:
            continue

        if attributes:
            if state_key not in item["attributes"]:
                continue
            state = item["attributes"][state_key]
        else:
            # Ignore data without correct keys
            if state_key not in item:
                continue

            # Unavailable or bad values
            if item[state_key] == "unavailable" or item[state_key] == "unknown":
                continue

            state = item[state_key]

        # Get the numerical key and the timestamp and ignore if in error
        try:
            state = float(state) * scale
            if pounds:
                state = dp2(state / 100)
            else:
                state = dp4(state)

        except (ValueError, TypeError):
            if isinstance(state, str):
                if state.lower() in ["on", "true", "yes"]:
                    state = 1
                elif state.lower() in ["off", "false", "no"]:
                    state = 0
                else:
                    continue
            else:
                continue

        try:
            last_updated_time = item[last_updated_key]
            last_updated_stamp = str2time(last_updated_time)
        except (ValueError, TypeError):
            continue

        day_stamp = last_updated_stamp.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        if offset_days:
            day_stamp += timedelta(days=offset_days)

        if first and daily and day_stamp == last_day_stamp:
            continue
        last_day_stamp = day_stamp

        # Add the state to the result
        if daily:
            # Convert day stamp from UTC into localtime
            results[day_stamp.strftime(TIME_FORMAT_DAILY)] = state
        else:
            results[last_updated_time] = state

    return results


def get_override_time_from_string(now_utc, time_str, plan_interval_minutes):
    """
    Convert a time string like "Sun 13:00" into a datetime object
    """
    # Parse the time string into a datetime object
    # Format is Sun 13:00
    try:
        override_time = datetime.strptime(time_str, "%a %H:%M")
        day_of_week_text = time_str.split()[0].lower()
        day_of_week = DAY_OF_WEEK_MAP.get(day_of_week_text, 0)
        has_day = True
    except ValueError:
        try:
            override_time = datetime.strptime(time_str, "%H:%M")
            day_of_week = now_utc.weekday()
            has_day = False
        except ValueError:
            return None

    # Convert day of week text to a number (0=Monday, 6=Sunday)
    day_of_week_today = now_utc.weekday()

    override_time = now_utc.replace(hour=override_time.hour, minute=override_time.minute, second=0, microsecond=0)

    # Ensure minutes are rounded down to the nearest plan_interval_minutes (e.g., 15 or 10)
    minute = (override_time.minute // plan_interval_minutes) * plan_interval_minutes
    override_time = override_time.replace(minute=minute)

    # Calculate days to add
    add_days = day_of_week - day_of_week_today

    # If the day has passed this week then use next week
    if add_days < 0:
        add_days += 7
    elif not has_day and override_time <= now_utc:
        add_days += 1

    override_time += timedelta(days=add_days)

    return override_time


def minute_data_state(history, days, now, state_key, last_updated_key, prev_last_updated_time=None):
    """
    Get historical data for state (e.g. predbat status)
    """
    mdata = {}
    last_state = "unknown"
    newest_state = "unknown"
    newest_age = 999999

    if not history:
        return mdata

    # Process history
    for item in history:
        # Ignore data without correct keys
        if state_key not in item:
            continue
        if last_updated_key not in item:
            continue

        # Unavailable or bad values
        if item[state_key] == "unavailable" or item[state_key] == "unknown":
            continue

        state = item[state_key]
        last_updated_time = str2time(item[last_updated_key])

        # Update prev to the first if not set
        if not prev_last_updated_time:
            prev_last_updated_time = last_updated_time
            last_state = state

        timed = now - last_updated_time
        timed_to = now - prev_last_updated_time

        minutes_to = int(timed_to.seconds / 60) + int(timed_to.days * 60 * 24)
        minutes = int(timed.seconds / 60) + int(timed.days * 60 * 24)

        minute = minutes
        while minute <= minutes_to:
            mdata[minute] = last_state
            minute += 1
        mdata[minutes] = state

        # Store previous state
        prev_last_updated_time = last_updated_time
        last_state = state

        if minutes <= newest_age:
            newest_age = minutes
            newest_state = state

    state = newest_state
    for minute in range(0, 60 * 24 * days):
        rindex = 60 * 24 * days - minute - 1
        state = mdata.get(rindex, state)
        mdata[rindex] = state

    return mdata


def history_attribute_to_minute_data(now_utc, data, backwards=True):
    """
    Get historical data for an attribute with history attribute filtering first
    """
    history = []
    oldest_date = now_utc
    for key in data:
        try:
            timestamp_key = str2time(key)
            oldest_date = min(oldest_date, timestamp_key)
        except (ValueError, TypeError) as e:
            continue

        value = data[key]
        history.append({"last_updated": key, "state": value})
    max_age = now_utc - oldest_date
    max_days = max(max_age.days, 1)
    mdata, _ = minute_data(history, max_days, now_utc, "state", "last_updated", backwards=backwards, smoothing=False, scale=1.0, clean_increment=False, required_unit=None)
    return [mdata, max_days]


def minute_data(
    history,
    days,
    now,
    state_key,
    last_updated_key,
    backwards=False,
    to_key=None,
    smoothing=False,
    clean_increment=False,
    divide_by=0,
    scale=1.0,
    accumulate=[],
    adjust_key=None,
    spreading=None,
    required_unit=None,
    prev_last_updated_time=None,
    last_state=0,
    attributes=False,
    max_increment=MAX_INCREMENT,
    interpolate=False,
):
    """
    Turns data from HA into a hash of data indexed by minute with the data being the value
    Can be backwards in time for history (N minutes ago) or forward in time (N minutes in the future)
    """
    mdata = {}
    adata = {}
    io_adjusted = {}
    newest_state = 0
    prev_state = 0
    newest_age = 999999

    # Bounds on the data we store
    minute_min = -days * 24 * 60
    minute_max = days * 24 * 60

    # Check history is valid, if not empty return
    if not history:
        return mdata, io_adjusted

    # Glitch filter, cleans glitches in the data and removes bad values, only for incrementing data
    if clean_increment and backwards:
        if len(history) > 2:
            prev_prev_item = history[0]
            prev_item = history[1]

            if state_key in prev_item and state_key in prev_prev_item:
                try:
                    prev_value = float(prev_item[state_key])
                except (ValueError, TypeError):
                    prev_value = 0

                try:
                    prev_prev_value = float(prev_prev_item[state_key])
                except (ValueError, TypeError):
                    prev_prev_value = 0

                for item in history[2:]:
                    try:
                        value = float(item[state_key])
                    except (ValueError, TypeError):
                        value = prev_value
                        item[state_key] = value

                    # Filter simple glitch
                    if (prev_value > value) and (prev_value > prev_prev_value) and abs(prev_value - value) >= 0.1 and (value >= prev_prev_value):
                        prev_item[state_key] = value
                        prev_value = value

                    prev_prev_item = prev_item
                    prev_prev_value = prev_value
                    prev_value = value
                    prev_item = item

    # Process history
    for item in history:
        if last_updated_key not in item:
            continue

        if attributes:
            if state_key not in item["attributes"]:
                continue
            if item["attributes"][state_key] == "unavailable" or item["attributes"][state_key] == "unknown":
                continue
            state = item["attributes"][state_key]
        else:
            # Ignore data without correct keys
            if state_key not in item:
                continue
            # Unavailable or bad values
            if item[state_key] == "unavailable" or item[state_key] == "unknown":
                continue
            state = item[state_key]

        # Get the numerical key and the timestamp and ignore if in error
        try:
            state = float(state) * scale
            last_updated_time = str2time(item[last_updated_key])
        except (ValueError, TypeError):
            continue

        # Find and converter units
        integrate = False
        if required_unit and ("attributes" in item):
            if "unit_of_measurement" in item["attributes"]:
                unit = item["attributes"]["unit_of_measurement"]
                if unit != required_unit:
                    if required_unit in ["kWh"] and unit in ["W"]:
                        state = state / 1000.0
                        integrate = True
                    elif required_unit in ["kWh"] and unit in ["kW"]:
                        integrate = True
                    elif required_unit in ["kW", "kWh", "kg", "kg/kWh"] and unit in ["W", "Wh", "g", "g/kWh"]:
                        state = state / 1000.0
                    elif required_unit in ["W", "Wh", "g", "g/kWh"] and unit in ["kW", "kWh", "kg", "kg/kWh"]:
                        state = state * 1000.0
                    else:
                        # Ignore data in wrong units if we can't converter
                        continue

        # Divide down the state if required
        if divide_by:
            state /= divide_by

        # Update prev to the first if not set
        if not prev_last_updated_time:
            prev_last_updated_time = last_updated_time
            last_state = state

        # Intelligent adjusted?
        if adjust_key:
            adjusted = item.get(adjust_key, False)
        else:
            adjusted = False

        # Work out end of time period
        # If we don't get it assume it's to the previous update, this is for historical data only (backwards)
        if to_key:
            to_value = item[to_key]
            if not to_value:
                to_time = now + timedelta(minutes=24 * 60 * days)
            else:
                to_time = str2time(item[to_key])
        else:
            if backwards:
                to_time = prev_last_updated_time
            else:
                if smoothing:
                    to_time = last_updated_time
                    last_updated_time = prev_last_updated_time
                else:
                    to_time = None

        if backwards:
            timed = now - last_updated_time
            if to_time:
                timed_to = now - to_time
        else:
            timed = last_updated_time - now
            if to_time:
                timed_to = to_time - now

        minutes = int(timed.total_seconds() / 60)
        if to_time:
            minutes_to = int(timed_to.total_seconds() / 60)

        if minutes < newest_age:
            newest_age = minutes
            prev_state = newest_state
            newest_state = state

        # Power to Energy
        if integrate and to_time:
            total_minutes = abs(minutes_to - minutes)
            state = last_state + state * total_minutes / 60.0

        if to_time:
            minute = minutes
            if minute == minutes_to:
                mdata[minute] = state
            else:
                if smoothing:
                    # Reset to zero, sometimes not exactly zero
                    if clean_increment and (state < last_state) and ((last_state - state) >= 1.0):
                        while minute < minutes_to:
                            if minute >= minute_min and minute <= minute_max:
                                mdata[minute] = state
                            minute += 1
                    else:
                        # Incrementing data can't go backwards
                        if clean_increment and state < last_state:
                            state = last_state

                        # Create linear function
                        diff = (state - last_state) / (minutes_to - minute)

                        # If the spike is too big don't smooth it, it will removed in the clean function later
                        if clean_increment and max_increment > 0 and diff > max_increment:
                            diff = 0

                        index = 0
                        while minute < minutes_to:
                            if minute >= minute_min and minute <= minute_max:
                                if backwards:
                                    mdata[minute] = state - diff * index
                                else:
                                    mdata[minute] = last_state + diff * index
                            minute += 1
                            index += 1
                else:
                    while minute < minutes_to:
                        if minute >= minute_min and minute <= minute_max:
                            if backwards:
                                mdata[minute] = last_state
                            else:
                                mdata[minute] = state

                            if adjusted:
                                adata[minute] = True
                        minute += 1
        else:
            if spreading:
                for minute in range(minutes, minutes + spreading):
                    if minute >= minute_min and minute <= minute_max:
                        mdata[minute] = state
            else:
                if minutes >= minute_min and minutes <= minute_max:
                    mdata[minutes] = state

        # Store previous time & state
        if to_time and not backwards:
            prev_last_updated_time = to_time
        else:
            prev_last_updated_time = last_updated_time
        last_state = state

    # If we only have a start time then fill the gaps with the last values
    if not to_key:
        # Fill from last sample until now with interpolation if enabled
        if interpolate and clean_increment and backwards:
            last_sample_minute = 0
            for minute in range(60):
                if minute in mdata:
                    last_sample_minute = minute
                    break
            last_but_one_sample_minute = last_sample_minute
            for minute in range(last_sample_minute + 5, 60):
                if minute in mdata and (mdata[minute] != mdata[last_sample_minute]):
                    last_but_one_sample_minute = minute
                    break
            sample_gap = last_but_one_sample_minute - last_sample_minute
            if last_sample_minute > 0 and sample_gap > 0 and last_sample_minute < 15:
                last_sample_value = mdata[last_sample_minute]
                last_but_one_minute_sample = mdata[last_but_one_sample_minute]
                step = (last_sample_value - last_but_one_minute_sample) / sample_gap
                if step > 0:
                    for minute in range(last_sample_minute):
                        mdata[minute] = dp4(last_sample_value + step * (last_sample_minute - minute))

        # Fill from last sample until now
        for minute in range(60 * 24 * days):
            if backwards:
                rindex = minute
            else:
                rindex = 60 * 24 * days - minute - 1

            if rindex not in mdata:
                mdata[rindex] = newest_state
            else:
                break

        # Fill gaps before the first value
        state = 0
        for minute in range(60 * 24 * days):
            if backwards:
                rindex = 60 * 24 * days - minute - 1
            else:
                rindex = minute
            if rindex in mdata:
                state = mdata[rindex]
                break

        # Fill gaps in the middle
        for minute in range(60 * 24 * days):
            if backwards:
                rindex = 60 * 24 * days - minute - 1
            else:
                rindex = minute
            state = mdata.get(rindex, state)
            mdata[rindex] = state

    # Reverse data with smoothing
    if clean_increment:
        mdata = clean_incrementing_reverse(mdata, max_increment)

    # Accumulate to previous data?
    if accumulate:
        for minute in range(60 * 24 * days):
            if minute in mdata:
                mdata[minute] += accumulate.get(minute, 0)
            else:
                mdata[minute] = accumulate.get(minute, 0)

    if adjust_key:
        io_adjusted = adata

    # Rounding
    for minute in mdata.keys():
        mdata[minute] = dp4(mdata[minute])

    return mdata, io_adjusted


def clean_incrementing_reverse(data, max_increment=0):
    """
    Cleanup an incrementing sensor data that runs backwards in time to remove the
    resets (where it goes back to 0) and make it always increment
    """
    new_data = {}
    length = max(data) + 1

    increment = 0
    last = data[length - 1]

    for index in range(length):
        rindex = length - index - 1
        nxt = data.get(rindex, last)
        if nxt >= last:
            if (max_increment > 0) and ((nxt - last) > max_increment):
                # Smooth out big spikes
                pass
            else:
                increment += nxt - last
            last = nxt
        elif nxt < last:
            if nxt <= 0 or ((last - nxt) >= (1.0)):
                last = nxt
        new_data[rindex] = increment

    return new_data


def format_time_ago(last_updated):
    """
    Format a timestamp to show how many minutes ago it was updated
    """
    if not last_updated:
        return "Never updated"

    try:
        now = datetime.now(timezone.utc)
        if not last_updated:
            return "Never updated"

        # Calculate time difference
        time_diff = now - last_updated
        total_minutes = int(time_diff.total_seconds() / 60)

        if total_minutes < 0:
            return "Just now"
        elif total_minutes == 0:
            return "Just now"
        elif total_minutes == 1:
            return "1 minute ago"
        elif total_minutes < 60:
            return f"{total_minutes} minutes ago"
        elif total_minutes < 120:
            return "1 hour ago"
        elif total_minutes < 1440:  # Less than 24 hours
            hours = total_minutes // 60
            return f"{hours} hours ago"
        else:  # More than 24 hours
            days = total_minutes // 1440
            if days == 1:
                return "1 day ago"
            else:
                return f"{days} days ago"

    except Exception as e:
        print(f"Error formatting time ago: {e}")
        return "Unknown ({})".format(last_updated)


def in_iboost_slot(minute, iboost_plan):
    """
    Is the given minute inside a car slot
    """
    load_amount = 0

    if iboost_plan:
        for slot in iboost_plan:
            start_minutes = slot["start"]
            end_minutes = slot["end"]
            kwh = slot["kwh"]
            slot_minutes = end_minutes - start_minutes
            slot_hours = slot_minutes / 60.0

            # Return the load in that slot
            if minute >= start_minutes and minute < end_minutes:
                load_amount = abs(kwh / slot_hours)
                break
    return load_amount


def in_car_slot(minute, num_cars, car_charging_slots):
    """
    Is the given minute inside a car slot
    """
    load_amount = [0 for car_n in range(num_cars)]

    for car_n in range(num_cars):
        if car_charging_slots[car_n]:
            for slot in car_charging_slots[car_n]:
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


def calc_kwh_limit(charge_limit, soc_max):
    """
    Calculate a charge limit in kwh
    """
    if isinstance(charge_limit, list):
        if soc_max <= 0:
            return [0 for i in range(len(charge_limit))]
        else:
            return [min(float(charge_limit[i]) / 100.0 * soc_max, soc_max) for i in range(len(charge_limit))]
    else:
        if soc_max <= 0:
            return 0
        else:
            return min(float(charge_limit) / 100.0 * soc_max, soc_max)


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
