# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# pyright: reportAttributeAccessIssue=false

from datetime import datetime, timedelta
from utils import minutes_to_time, str2time, dp0, dp1, dp2, dp3, dp4, time_string_to_stamp
from config import MAX_INCREMENT, MINUTE_WATT, PREDICT_STEP, TIME_FORMAT, TIME_FORMAT_SECONDS, PREDBAT_MODE_OPTIONS, PREDBAT_MODE_CONTROL_SOC, PREDBAT_MODE_CONTROL_CHARGEDISCHARGE, PREDBAT_MODE_CONTROL_CHARGE, PREDBAT_MODE_MONITOR, TIME_FORMAT_DAILY
from futurerate import FutureRate


class Fetch:
    def get_cloud_factor(self, minutes_now, pv_data, pv_data10):
        """
        Work out approximated cloud factor
        """
        pv_total = 0
        pv_total10 = 0
        for minute in range(self.forecast_minutes):
            pv_total += pv_data.get(minute + minutes_now, 0.0)
            pv_total10 += pv_data10.get(minute + minutes_now, 0.0)

        pv_factor = None
        if pv_total > 0 and (pv_total > pv_total10):
            pv_diff = pv_total - pv_total10
            pv_factor = dp2(pv_diff / pv_total) / 2.0
            pv_factor = min(pv_factor, 1.0)

        if self.metric_cloud_enable:
            pv_factor_round = pv_factor
            if pv_factor_round:
                pv_factor_round = dp1(pv_factor_round)
            self.log("PV Forecast {} kWh and 10% Forecast {} kWh pv cloud factor {}".format(dp1(pv_total), dp1(pv_total10), pv_factor_round))
            return pv_factor
        else:
            return None

    def filtered_today(self, time_data, resetmidnight=False, stamp=None):
        """
        Grab figure for today (midnight)
        """
        if stamp is None:
            stamp = self.midnight_utc + timedelta(days=1)

        stamp_minus = stamp - timedelta(minutes=PREDICT_STEP)
        if resetmidnight:
            stamp = stamp_minus
        tomorrow_value = time_data.get(stamp.strftime(TIME_FORMAT), time_data.get(stamp_minus.strftime(TIME_FORMAT), None))
        return tomorrow_value

    def filtered_times(self, time_data):
        """
        Filter out duplicate values in time series data
        """

        prev = None
        new_data = {}
        keys = list(time_data.keys())
        for id in range(len(keys)):
            stamp = keys[id]
            value = time_data[stamp]
            next_value = time_data[keys[id + 1]] if id + 1 < len(keys) else None
            if prev is None or value != prev or next_value != value:
                new_data[stamp] = value
            prev = value
            id += 1
        return new_data

    def step_data_history(
        self,
        item,
        minutes_now,
        forward,
        step=PREDICT_STEP,
        scale_today=1.0,
        scale_fixed=1.0,
        type_load=False,
        load_forecast={},
        cloud_factor=None,
        load_scaling_dynamic=None,
        base_offset=None,
        flip=False,
    ):
        """
        Create cached step data for historical array
        """
        values = {}
        cloud_diff = 0

        for minute in range(0, self.forecast_minutes + 30, step):
            value = 0
            minute_absolute = minute + minutes_now

            scaling_dynamic = 1.0
            if load_scaling_dynamic:
                scaling_dynamic = load_scaling_dynamic.get(minute_absolute, scaling_dynamic)

            # Reset in-day adjustment for tomorrow
            if (minute + minutes_now) > 24 * 60:
                scale_today = 1.0

            if type_load and not forward:
                if self.load_forecast_only:
                    load_yesterday, load_yesterday_raw = (0, 0)
                else:
                    load_yesterday, load_yesterday_raw = self.get_filtered_load_minute(item, minute, historical=True, step=step)
                value += load_yesterday
            else:
                for offset in range(step):
                    if forward:
                        value += item.get(minute + minutes_now + offset, 0.0)
                    else:
                        if base_offset:
                            value += self.get_historical_base(item, minute + offset, base_offset)
                        else:
                            value += self.get_historical(item, minute + offset)

            # Extra load adding in (e.g. heat pump)
            load_extra = 0
            if load_forecast:
                for offset in range(step):
                    load_extra += self.get_from_incrementing(load_forecast, minute_absolute, backwards=False)
            values[minute] = dp4((value + load_extra) * scaling_dynamic * scale_today * scale_fixed)

        # Simple divergence model keeps the same total but brings PV/Load up and down every 5 minutes
        if cloud_factor and cloud_factor > 0:
            for minute in range(0, self.forecast_minutes, step):
                cloud_on = (int((minute + self.minutes_now) / 5) + 1 if flip else 0) % 2
                if cloud_on > 0:
                    cloud_diff += min(values[minute] * cloud_factor, values.get(minute + 5, 0) * cloud_factor)
                    values[minute] += cloud_diff
                else:
                    subtract = min(cloud_diff, values[minute])
                    values[minute] -= subtract
                    cloud_diff = 0
                values[minute] = dp4(values[minute])

        return values

    def clean_incrementing_reverse(self, data, max_increment=0):
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

    def get_filtered_load_minute(self, data, minute_previous, historical, step=1):
        """
        Gets a previous load minute after filtering for car charging
        """
        load_yesterday_raw = 0

        for offset in range(step):
            if historical:
                load_yesterday_raw += self.get_historical(data, minute_previous + offset)
            else:
                load_yesterday_raw += self.get_from_incrementing(data, minute_previous + offset)

        load_yesterday = load_yesterday_raw

        # Subtract car charging energy and iboost energy (if enabled)
        subtract_energy = 0
        for offset in range(step):
            if historical:
                if self.car_charging_hold and self.car_charging_energy:
                    subtract_energy += self.get_historical(self.car_charging_energy, minute_previous + offset)
                if self.iboost_energy_subtract and self.iboost_energy_today:
                    subtract_energy += self.get_historical(self.iboost_energy_today, minute_previous + offset)
            else:
                if self.car_charging_hold and self.car_charging_energy:
                    subtract_energy += self.get_from_incrementing(self.car_charging_energy, minute_previous + offset)
                if self.iboost_energy_subtract and self.iboost_energy_today:
                    subtract_energy += self.get_from_incrementing(self.iboost_energy_today, minute_previous + offset)
        load_yesterday = max(0, load_yesterday - subtract_energy)

        if self.car_charging_hold and (not self.car_charging_energy) and (load_yesterday >= (self.car_charging_threshold * step)):
            # Car charging hold - ignore car charging in computation based on threshold
            load_yesterday = max(load_yesterday - (self.car_charging_rate[0] * step / 60.0), 0)

        # Apply base load
        base_load = self.base_load * step / 60.0
        if load_yesterday < base_load:
            add_to_base = base_load - load_yesterday
            load_yesterday += add_to_base
            load_yesterday_raw += add_to_base

        return load_yesterday, load_yesterday_raw

    def previous_days_modal_filter(self, data):
        """
        Look at the data from previous days and discard the best case one
        """

        total_points = len(self.days_previous)
        sum_days = []
        sum_days_id = {}
        min_sum = 99999999
        min_sum_day = 0

        idx = 0
        for days in self.days_previous:
            use_days = max(min(days, self.load_minutes_age), 1)
            sum_day = 0
            full_days = 24 * 60 * (use_days - 1)
            for minute in range(0, 24 * 60, PREDICT_STEP):
                minute_previous = 24 * 60 - minute + full_days
                load_yesterday, load_yesterday_raw = self.get_filtered_load_minute(data, minute_previous, historical=False, step=PREDICT_STEP)
                sum_day += load_yesterday
            sum_days.append(dp2(sum_day))
            sum_days_id[days] = sum_day
            if sum_day < min_sum:
                min_sum_day = days
                min_sum_day_idx = idx
                min_sum = dp2(sum_day)
            idx += 1

        self.log("Historical data totals for days {} are {} - min {}".format(self.days_previous, sum_days, min_sum))
        if self.load_filter_modal and total_points >= 3 and (min_sum_day > 0):
            self.log("Model filter enabled - Discarding day {} as it is the lowest of the {} datapoints".format(min_sum_day, len(self.days_previous)))
            del self.days_previous[min_sum_day_idx]
            del self.days_previous_weight[min_sum_day_idx]

        # Gap filling
        gap_size = max(self.get_arg("load_filter_threshold", 30), 5)
        for days in self.days_previous:
            use_days = max(min(days, self.load_minutes_age), 1)
            num_gaps = 0
            full_days = 24 * 60 * (use_days - 1)
            for minute in range(0, 24 * 60, PREDICT_STEP):
                minute_previous = 24 * 60 - minute + full_days
                if data.get(minute_previous, 0) == data.get(minute_previous + gap_size, 0):
                    num_gaps += PREDICT_STEP

            # If we have some gaps
            if num_gaps > 0:
                average_day = sum_days_id[days]
                if (average_day == 0) or (num_gaps >= 24 * 60):
                    self.log("Warn: Historical day {} has no data, unable to fill gaps normally using nominal 24kWh - you should fix your system!".format(days))
                    average_day = 24.0
                else:
                    real_data_percent = ((24 * 60) - num_gaps) / (24 * 60)
                    average_day /= real_data_percent
                    self.log("Warn: Historical day {} has {} minutes of gap in the data, filled from {} kWh to make new average {} kWh (percent {}%)".format(days, num_gaps, dp2(sum_days_id[days]), dp2(average_day), dp0(real_data_percent * 100.0)))

                # Do the filling
                per_minute_increment = average_day / (24 * 60)
                for minute in range(0, 24 * 60, PREDICT_STEP):
                    minute_previous = 24 * 60 - minute + full_days
                    if data.get(minute_previous, 0) == data.get(minute_previous + gap_size, 0):
                        for offset in range(minute_previous, 0, -1):
                            if offset in data:
                                data[offset] += per_minute_increment * PREDICT_STEP
                            else:
                                data[offset] = per_minute_increment * PREDICT_STEP

    def get_historical_base(self, data, minute, base_minutes):
        """
        Get historical data from base minute ago
        """
        # No data?
        if not data:
            return 0

        minute_previous = base_minutes - minute
        return self.get_from_incrementing(data, minute_previous)

    def get_historical(self, data, minute):
        """
        Get historical data across N previous days in days_previous array based on current minute
        """
        total = 0
        total_weight = 0
        this_point = 0

        # No data?
        if not data:
            return 0

        for days in self.days_previous:
            use_days = max(min(days, self.load_minutes_age), 1)
            weight = self.days_previous_weight[this_point]
            full_days = 24 * 60 * (use_days - 1)
            minute_previous = 24 * 60 - minute + full_days
            value = self.get_from_incrementing(data, minute_previous)
            total += value * weight
            total_weight += weight
            this_point += 1

        # Zero data?
        if total_weight == 0:
            return 0
        else:
            return total / total_weight

    def get_from_incrementing(self, data, index, backwards=True):
        """
        Get a single value from an incrementing series e.g. kWh today -> kWh this minute
        """
        while index < 0:
            index += 24 * 60
        if backwards:
            return max(data.get(index, 0) - data.get(index + 1, 0), 0)
        else:
            return max(data.get(index + 1, 0) - data.get(index, 0), 0)

    def minute_data_import_export(self, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True):
        """
        Download one or more entities for import/export data
        """
        if "." not in key:
            entity_ids = self.get_arg(key, indirect=False)
        else:
            entity_ids = key

        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        if entity_ids is None:
            self.log("Error: No entity IDs provided for {}".format(key))
            entity_ids = []

        import_today = {}
        for entity_id in entity_ids:
            try:
                history = self.get_history_wrapper(entity_id=entity_id, days=self.max_days_previous)
            except (ValueError, TypeError):
                history = []

            if history:
                import_today = self.minute_data(
                    history[0],
                    self.max_days_previous,
                    now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    smoothing=smoothing,
                    scale=scale,
                    clean_increment=increment,
                    accumulate=import_today,
                    required_unit=required_unit,
                )
            else:
                self.log("Error: Unable to fetch history for {}".format(entity_id))
                self.record_status("Error: Unable to fetch history from {}".format(entity_id), had_errors=True)
                raise ValueError

        return import_today

    def minute_data_load(self, now_utc, entity_name, max_days_previous, load_scaling=1.0, required_unit=None):
        """
        Download one or more entities for load data
        """
        entity_ids = self.get_arg(entity_name, indirect=False)
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        load_minutes = {}
        age_days = None
        for entity_id in entity_ids:
            try:
                history = self.get_history_wrapper(entity_id=entity_id, days=max_days_previous)
            except (ValueError, TypeError):
                history = []

            if history:
                item = history[0][0]
                try:
                    last_updated_time = str2time(item["last_updated"])
                except (ValueError, TypeError):
                    last_updated_time = now_utc
                age = now_utc - last_updated_time
                if age_days is None:
                    age_days = age.days
                else:
                    age_days = min(age_days, age.days)
                load_minutes = self.minute_data(
                    history[0],
                    max_days_previous,
                    now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    smoothing=True,
                    scale=load_scaling,
                    clean_increment=True,
                    accumulate=load_minutes,
                    required_unit=required_unit,
                )
            else:
                self.log("Error: Unable to fetch history for {}".format(entity_id))
                self.record_status("Error: Unable to fetch history from {}".format(entity_id), had_errors=True)
                raise ValueError

        if age_days is None:
            age_days = 0
        return load_minutes, age_days

    def minute_data_state(self, history, days, now, state_key, last_updated_key, prev_last_updated_time=None):
        """
        Get historical data for state (e.g. predbat status)
        """
        mdata = {}
        last_state = "unknown"
        newest_state = 0
        last_state = 0
        newest_age = 999999

        if not history:
            self.log("Warn: Empty history passed to minute_data_state, ignoring (check your settings)...")
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
            while minute < minutes_to:
                mdata[minute] = last_state
                minute += 1

            # Store previous state
            prev_last_updated_time = last_updated_time
            last_state = state

            if minutes < newest_age:
                newest_age = minutes
                newest_state = state

        state = newest_state
        for minute in range(0, 60 * 24 * days):
            rindex = 60 * 24 * days - minute - 1
            state = mdata.get(rindex, state)
            mdata[rindex] = state
            minute += 1

        return mdata

    def history_attribute_to_minute_data(self, data, backwards=True):
        """
        Get historical data for an attribute with history attribute filtering first
        """
        history = []
        oldest_date = self.now_utc
        for key in data:
            try:
                timestamp_key = str2time(key)
                oldest_date = min(oldest_date, timestamp_key)
            except (ValueError, TypeError) as e:
                continue

            value = data[key]
            history.append({"last_updated": key, "state": value})
        max_age = self.now_utc - oldest_date
        max_days = max(max_age.days, 1)
        return [self.minute_data(history, max_days, self.now_utc, "state", "last_updated", backwards=backwards, smoothing=False, scale=1.0, clean_increment=False, required_unit=None), max_days]

    def minute_data(
        self,
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
    ):
        """
        Turns data from HA into a hash of data indexed by minute with the data being the value
        Can be backwards in time for history (N minutes ago) or forward in time (N minutes in the future)
        """
        mdata = {}
        adata = {}
        newest_state = 0
        newest_age = 999999
        max_increment = MAX_INCREMENT

        # Bounds on the data we store
        minute_min = -days * 24 * 60
        minute_max = days * 24 * 60

        # Check history is valid
        if not history:
            self.log("Warn: Empty history passed to minute_data, ignoring (check your settings)...")
            return mdata

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
                    to_time = now + timedelta(minutes=24 * 60 * self.forecast_days)
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
            mdata = self.clean_incrementing_reverse(mdata, max_increment)

        # Accumulate to previous data?
        if accumulate:
            for minute in range(60 * 24 * days):
                if minute in mdata:
                    mdata[minute] += accumulate.get(minute, 0)
                else:
                    mdata[minute] = accumulate.get(minute, 0)

        if adjust_key:
            self.io_adjusted = adata

        # Rounding
        for minute in mdata.keys():
            mdata[minute] = dp4(mdata[minute])

        return mdata

    def prune_today(self, data, prune=True, group=15, prune_future=False, intermediate=False):
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
            if not prune or (timekey > self.midnight_utc):
                if prune_future and (timekey > self.now_utc):
                    continue
                results[key] = data[key]
                last_time = timekey
                prev_value = data[key]
        return results

    def history_attribute(self, history, state_key="state", last_updated_key="last_updated", scale=1.0, attributes=False, daily=False, offset_days=0, first=True, pounds=False):
        results = {}
        last_updated_time = None
        last_day_stamp = None

        if not isinstance(history, list):
            return results

        if history and len(history) >= 1:
            history = history[0]

        if not isinstance(history, list):
            self.log("Warn: history_attribute expects a list of history items, got {}".format(type(history)))
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

    def fetch_sensor_data(self):
        """
        Fetch all the data, e.g. energy rates, load, PV predictions, car plan etc.
        """
        self.rate_import = {}
        self.rate_import_replicated = {}
        self.rate_export = {}
        self.rate_export_replicated = {}
        self.rate_slots = []
        self.io_adjusted = {}
        self.low_rates = []
        self.high_export_rates = []
        self.octopus_slots = []
        self.cost_today_sofar = 0
        self.carbon_today_sofar = 0
        self.import_today = {}
        self.export_today = {}
        self.battery_temperature_history = {}
        self.battery_temperature_prediction = {}
        self.pv_today = {}
        self.load_minutes = {}
        self.load_minutes_age = 0
        self.load_forecast = {}
        self.load_forecast_array = []
        self.pv_forecast_minute = {}
        self.pv_forecast_minute10 = {}
        self.load_scaling_dynamic = {}
        self.carbon_intensity = {}
        self.carbon_history = {}
        self.octopus_free_slots = {}
        self.octopus_saving_slots = {}

        # Alert feed if enabled
        self.process_alerts()

        # iBoost load data
        if "iboost_energy_today" in self.args:
            self.iboost_energy_today, iboost_energy_age = self.minute_data_load(self.now_utc, "iboost_energy_today", self.max_days_previous, required_unit="kWh", load_scaling=1.0)
            if iboost_energy_age >= 1:
                self.iboost_today = dp2(abs(self.iboost_energy_today[0] - self.iboost_energy_today[self.minutes_now]))
                self.log("iBoost energy today from sensor reads {} kWh".format(self.iboost_today))

        # Fetch extra load forecast
        self.load_forecast, self.load_forecast_array = self.fetch_extra_load_forecast(self.now_utc)

        # Load previous load data
        if self.get_arg("ge_cloud_data", False):
            self.download_ge_data(self.now_utc)
        else:
            # Load data
            if "load_today" in self.args:
                self.load_minutes, self.load_minutes_age = self.minute_data_load(self.now_utc, "load_today", self.max_days_previous, required_unit="kWh", load_scaling=self.load_scaling)
                self.log("Found {} load_today datapoints going back {} days".format(len(self.load_minutes), self.load_minutes_age))
                self.load_minutes_now = max(self.load_minutes.get(0, 0) - self.load_minutes.get(self.minutes_now, 0), 0)
            else:
                if self.load_forecast:
                    self.log("Using load forecast from load_forecast sensor")
                    self.load_minutes_now = self.load_forecast.get(0, 0)
                    self.load_minutes_age = 0
                else:
                    self.log("Error: You have not set load_today or load_forecast, you will have no load data")
                    self.record_status(message="Error: load_today not set correctly", had_errors=True)
                    raise ValueError

            # Load import today data
            if "import_today" in self.args:
                self.import_today = self.minute_data_import_export(self.now_utc, "import_today", scale=self.import_export_scaling, required_unit="kWh")
                self.import_today_now = max(self.import_today.get(0, 0) - self.import_today.get(self.minutes_now, 0), 0)
            else:
                self.log("Warn: You have not set import_today in apps.yaml, you will have no previous import data")

            # Load export today data
            if "export_today" in self.args:
                self.export_today = self.minute_data_import_export(self.now_utc, "export_today", scale=self.import_export_scaling, required_unit="kWh")
                self.export_today_now = max(self.export_today.get(0, 0) - self.export_today.get(self.minutes_now, 0), 0)
            else:
                self.log("Warn: You have not set export_today in apps.yaml, you will have no previous export data")

            # PV today data
            if "pv_today" in self.args:
                self.pv_today = self.minute_data_import_export(self.now_utc, "pv_today", required_unit="kWh")
                self.pv_today_now = max(self.pv_today.get(0, 0) - self.pv_today.get(self.minutes_now, 0), 0)
            else:
                self.log("Warn: You have not set pv_today in apps.yaml, you will have no previous pv data")

        # Battery temperature
        if "battery_temperature_history" in self.args:
            self.battery_temperature_history = self.minute_data_import_export(self.now_utc, "battery_temperature_history", scale=1.0, increment=False, smoothing=False)
            data = []
            for minute in range(0, 24 * 60, 5):
                data.append({minute: self.battery_temperature_history.get(minute, 0)})
            self.battery_temperature_prediction = self.predict_battery_temperature(self.battery_temperature_history, step=PREDICT_STEP)
            self.log("Fetched battery temperature history data, current temperature {}".format(self.battery_temperature_history.get(0, None)))

        # Car charging hold - when enabled battery is held during car charging in simulation
        self.car_charging_energy = self.load_car_energy(self.now_utc)

        # Log current values
        self.log("Current data so far today: load {} kWh import {} kWh export {} kWh pv {} kWh".format(dp2(self.load_minutes_now), dp2(self.import_today_now), dp2(self.export_today_now), dp2(self.pv_today_now)))

        if "rates_import_octopus_url" in self.args:
            # Fixed URL for rate import
            self.log("Downloading import rates directly from URL {}".format(self.get_arg("rates_import_octopus_url", indirect=False)))
            self.rate_import = self.download_octopus_rates(self.get_arg("rates_import_octopus_url", indirect=False))
        elif self.octopus_api_direct:
            self.log("Downloading import rates directly from Octopus API")
            self.rate_import = self.get_octopus_direct("import")
            if not self.rate_import:
                self.log("Error: Unable to download rates from Octopus API")
                self.record_status(message="Error: Unable to download rates from Octopus API", had_errors=True)
                raise ValueError
        elif "metric_octopus_import" in self.args:
            # Octopus import rates
            entity_id = self.get_arg("metric_octopus_import", None, indirect=False)
            self.rate_import = self.fetch_octopus_rates(entity_id, adjust_key="is_intelligent_adjusted")
            if not self.rate_import:
                self.log("Error: metric_octopus_import is not set correctly or no energy rates can be read")
                self.record_status(message="Error: metric_octopus_import not set correctly or no energy rates can be read", had_errors=True)
                raise ValueError
        elif "metric_energidataservice_import" in self.args:
            # Octopus import rates
            entity_id = self.get_arg("metric_energidataservice_import", None, indirect=False)
            self.rate_import = self.fetch_energidataservice_rates(entity_id, adjust_key="is_intelligent_adjusted")
            if not self.rate_import:
                self.log("Error: metric_energidataservice_import is not set correctly or no energy rates can be read")
                self.record_status(message="Error: metric_energidataservice_import not set correctly or no energy rates can be read", had_errors=True)
                raise ValueError
        else:
            # Basic rates defined by user over time
            self.rate_import = self.basic_rates(self.get_arg("rates_import", [], indirect=False), "rates_import")

        # Gas rates if set
        if self.octopus_api_direct:
            self.log("Downloading gas rates directly from Octopus API")
            self.rate_gas = self.get_octopus_direct("gas")
            self.rate_gas, self.rate_gas_replicated = self.rate_replicate(self.rate_gas, is_import=False, is_gas=False)
            self.rate_scan_gas(self.rate_gas, print=True)
        elif "metric_octopus_gas" in self.args:
            entity_id = self.get_arg("metric_octopus_gas", None, indirect=False)
            self.rate_gas = self.fetch_octopus_rates(entity_id)
            if not self.rate_gas:
                self.log("Error: metric_octopus_gas is not set correctly or no energy rates can be read")
                self.record_status(message="Error: rate_import_gas not set correctly or no energy rates can be read", had_errors=True)
                raise ValueError
            self.rate_gas, self.rate_gas_replicated = self.rate_replicate(self.rate_gas, is_import=False, is_gas=False)
            self.rate_scan_gas(self.rate_gas, print=True)
        elif "rates_gas" in self.args:
            self.rate_gas = self.basic_rates(self.get_arg("rates_gas", [], indirect=False), "rates_gas")
            self.rate_gas, self.rate_gas_replicated = self.rate_replicate(self.rate_gas, is_import=False, is_gas=False)
            self.rate_scan_gas(self.rate_gas, print=True)

        # Carbon intensity data
        if self.carbon_enable and ("carbon_intensity" in self.args):
            entity_id = self.get_arg("carbon_intensity", None, indirect=False)
            self.carbon_intensity, self.carbon_history = self.fetch_carbon_intensity(entity_id)

        # SOC history
        soc_kwh_data = self.get_history_wrapper(entity_id=self.prefix + ".soc_kw_h0", days=2, required=False)
        if soc_kwh_data:
            self.soc_kwh_history = self.minute_data(
                soc_kwh_data[0],
                2,
                self.now_utc,
                "state",
                "last_updated",
                backwards=True,
                clean_increment=False,
                smoothing=False,
                divide_by=1.0,
                scale=1.0,
                required_unit="kWh",
            )

        # Work out current car SoC and limit
        self.car_charging_loss = 1 - float(self.get_arg("car_charging_loss"))

        if (self.octopus_api_direct and self.octopus_api_direct.get_intelligent_device()) or (not self.octopus_api_direct and ("octopus_intelligent_slot" in self.args)):
            completed = []
            planned = []
            vehicle = {}
            vehicle_pref = {}

            if self.octopus_api_direct:
                completed = self.octopus_api_direct.get_intelligent_completed_dispatches()
                planned = self.octopus_api_direct.get_intelligent_planned_dispatches()
                vehicle = self.octopus_api_direct.get_intelligent_vehicle()
                vehicle_pref = vehicle
                self.log("Octopus API planned and completed slots: {} - {}".format(completed, planned))
            else:
                entity_id = self.get_arg("octopus_intelligent_slot", indirect=False)
                if entity_id and "octopus_intelligent_slot_action_config" in self.args:
                    config_entry = self.get_arg("octopus_intelligent_slot_action_config", None, indirect=False)
                    service_name = entity_id.replace(".", "/")
                    result = self.call_service_wrapper(service_name, config_entry=config_entry, return_response=True)
                    if result and ("slots" in result):
                        planned = result["slots"]
                    else:
                        self.log("Warn: Unable to get data from {} - octopus_intelligent_slot using action config {} result was {}".format(entity_id, config_entry, result))
                else:
                    try:
                        completed = self.get_state_wrapper(entity_id=entity_id, attribute="completedDispatches") or self.get_state_wrapper(entity_id=entity_id, attribute="completed_dispatches")
                        planned = self.get_state_wrapper(entity_id=entity_id, attribute="plannedDispatches") or self.get_state_wrapper(entity_id=entity_id, attribute="planned_dispatches")
                        vehicle = self.get_state_wrapper(entity_id=entity_id, attribute="registeredKrakenflexDevice")
                        vehicle_pref = self.get_state_wrapper(entity_id=entity_id, attribute="vehicleChargingPreferences")
                    except (ValueError, TypeError):
                        self.log("Warn: Unable to get data from {} - octopus_intelligent_slot may not be set correctly".format(entity_id))
                        self.record_status(message="Error: octopus_intelligent_slot not set correctly", had_errors=True)

            # Completed and planned slots
            if completed:
                self.octopus_slots += completed
            if planned and (not self.octopus_intelligent_ignore_unplugged or self.car_charging_planned[0]):
                # We only count planned slots if the car is plugged in or we are ignoring unplugged cars
                self.octopus_slots += planned

            # Get rate for import to compute charging costs
            if self.rate_import:
                self.rate_scan(self.rate_import, print=False)

            if self.num_cars >= 1:
                # Extract vehicle data if we can get it
                if vehicle or self.octopus_api_direct:
                    self.car_charging_battery_size[0] = float(vehicle.get("vehicleBatterySizeInKwh", self.car_charging_battery_size[0]))
                    self.car_charging_rate[0] = float(vehicle.get("chargePointPowerInKw", self.car_charging_rate[0]))
                else:
                    size = self.get_state_wrapper(entity_id=entity_id, attribute="vehicle_battery_size_in_kwh")
                    rate = self.get_state_wrapper(entity_id=entity_id, attribute="charge_point_power_in_kw")
                    if size:
                        self.car_charging_battery_size[0] = size
                    if rate:
                        self.car_charging_rate[0] = rate

                # Get car charging limit again from car based on new battery size
                self.car_charging_limit[0] = dp3((float(self.get_arg("car_charging_limit", 100.0, index=0)) * self.car_charging_battery_size[0]) / 100.0)

                # Extract vehicle preference if we can get it
                if (vehicle_pref or self.octopus_api_direct) and self.octopus_intelligent_charging:
                    octopus_limit = max(float(vehicle_pref.get("weekdayTargetSoc", 100)), float(vehicle_pref.get("weekendTargetSoc", 100)))
                    octopus_ready_time = vehicle_pref.get("weekdayTargetTime", None)
                    if not octopus_ready_time:
                        octopus_ready_time = self.car_charging_plan_time[0]
                    else:
                        if isinstance(octopus_ready_time, str) and len(octopus_ready_time) == 5:
                            octopus_ready_time += ":00"
                    self.car_charging_plan_time[0] = octopus_ready_time
                    octopus_limit = dp3(octopus_limit * self.car_charging_battery_size[0] / 100.0)
                    self.car_charging_limit[0] = min(self.car_charging_limit[0], octopus_limit)
                elif self.octopus_intelligent_charging:
                    octopus_ready_time = self.get_arg("octopus_ready_time", None)
                    if isinstance(octopus_ready_time, str) and len(octopus_ready_time) == 5:
                        octopus_ready_time += ":00"
                    octopus_limit = self.get_arg("octopus_charge_limit", None)
                    if octopus_limit:
                        try:
                            octopus_limit = float(octopus_limit)
                        except (ValueError, TypeError):
                            self.log("Warn: octopus_charge_limit is set to a bad value {} in apps.yaml, must be a number".format(octopus_limit))
                            octopus_limit = None
                    if octopus_limit:
                        octopus_limit = dp3(float(octopus_limit) * self.car_charging_battery_size[0] / 100.0)
                        self.car_charging_limit[0] = min(self.car_charging_limit[0], octopus_limit)
                    if octopus_ready_time:
                        self.car_charging_plan_time[0] = octopus_ready_time

                # Use octopus slots for charging?
                if self.octopus_intelligent_charging:
                    self.octopus_slots = self.add_now_to_octopus_slot(self.octopus_slots, self.now_utc)
                    if not self.octopus_intelligent_ignore_unplugged or self.car_charging_planned[0]:
                        self.car_charging_slots[0] = self.load_octopus_slots(self.octopus_slots, self.octopus_intelligent_consider_full)
                        if self.car_charging_slots[0]:
                            self.log("Car 0 using Octopus Intelligent, charging planned - charging limit {}, ready time {} - battery size {}".format(self.car_charging_limit[0], self.car_charging_plan_time[0], self.car_charging_battery_size[0]))
                            self.car_charging_planned[0] = True
                        else:
                            self.log("Car 0 using Octopus Intelligent, not charging is planned")
                            self.car_charging_planned[0] = False
                    else:
                        self.log("Car 0 using Octopus Intelligent is unplugged")
                        self.car_charging_planned[0] = False
        else:
            # Disable octopus charging if we don't have the slot sensor
            self.octopus_intelligent_charging = False

        # Work out car SoC and reset next
        self.car_charging_soc = [0.0 for car_n in range(self.num_cars)]
        self.car_charging_soc_next = [None for car_n in range(self.num_cars)]
        for car_n in range(self.num_cars):
            if (car_n == 0) and self.car_charging_manual_soc:
                self.car_charging_soc[car_n] = self.get_arg("car_charging_manual_soc_kwh")
            else:
                self.car_charging_soc[car_n] = (self.get_arg("car_charging_soc", 0.0, index=car_n) * self.car_charging_battery_size[car_n]) / 100.0
        if self.num_cars:
            self.log("Cars: SoC kWh: {} Charge limit {} plan time {} battery size {}".format(self.car_charging_soc, self.car_charging_limit, self.car_charging_plan_time, self.car_charging_battery_size))

        if "rates_export_octopus_url" in self.args:
            # Fixed URL for rate export
            self.log("Downloading export rates directly from URL {}".format(self.get_arg("rates_export_octopus_url", indirect=False)))
            self.rate_export = self.download_octopus_rates(self.get_arg("rates_export_octopus_url", indirect=False))
        elif self.octopus_api_direct:
            self.log("Downloading export rates directly from Octopus API")
            self.rate_export = self.get_octopus_direct("export")
        elif "metric_octopus_export" in self.args:
            # Octopus export rates
            entity_id = self.get_arg("metric_octopus_export", None, indirect=False)
            self.rate_export = self.fetch_octopus_rates(entity_id)
            if not self.rate_export:
                self.log("Warning: metric_octopus_export is not set correctly or no energy rates can be read")
                self.record_status(message="Error: metric_octopus_export not set correctly or no energy rates can be read", had_errors=True)
        elif "metric_energidataservice_export" in self.args:
            # Octopus export rates
            entity_id = self.get_arg("metric_energidataservice_export", None, indirect=False)
            self.rate_export = self.fetch_energidataservice_rates(entity_id)
            if not self.rate_export:
                self.log("Warning: metric_energidataservice_export is not set correctly or no energy rates can be read")
                self.record_status(message="Error: metric_energidataservice_export not set correctly or no energy rates can be read", had_errors=True)
        else:
            # Basic rates defined by user over time
            self.rate_export = self.basic_rates(self.get_arg("rates_export", [], indirect=False), "rates_export")

        # Fetch octopus saving sessions and free sessions
        self.octopus_free_slots, self.octopus_saving_slots = self.fetch_octopus_sessions()

        # Standing charge
        if self.octopus_api_direct:
            self.metric_standing_charge = self.get_standing_charge_direct()
            self.log("Octopus Import standing charge is set to {} p".format(self.metric_standing_charge))
        else:
            self.metric_standing_charge = self.get_arg("metric_standing_charge", 0.0) * 100.0
            self.log("Standing charge is set to {} p".format(self.metric_standing_charge))

        # futurerate data
        futurerate = FutureRate(self)
        self.future_energy_rates_import, self.future_energy_rates_export = futurerate.futurerate_analysis(self.rate_import, self.rate_export)

        # Replicate and scan import rates
        if self.rate_import:
            self.rate_scan(self.rate_import, print=False)
            self.rate_import, self.rate_import_replicated = self.rate_replicate(self.rate_import, self.io_adjusted, is_import=True)
            self.rate_import = self.rate_add_io_slots(self.rate_import, self.octopus_slots)
            self.load_saving_slot(self.octopus_saving_slots, export=False, rate_replicate=self.rate_import_replicated)
            self.load_free_slot(self.octopus_free_slots, export=False, rate_replicate=self.rate_import_replicated)
            self.rate_import = self.basic_rates(self.get_arg("rates_import_override", [], indirect=False), "rates_import_override", self.rate_import, self.rate_import_replicated)
            self.rate_import = self.apply_manual_rates(self.rate_import, self.manual_import_rates, is_import=True, rate_replicate=self.rate_import_replicated)
            self.rate_scan(self.rate_import, print=True)
        else:
            self.log("Warning: No import rate data provided")
            self.record_status(message="Error: No import rate data provided", had_errors=True)

        # Replicate and scan export rates
        if self.rate_export:
            self.rate_scan_export(self.rate_export, print=False)
            self.rate_export, self.rate_export_replicated = self.rate_replicate(self.rate_export, is_import=False)
            # For export tariff only load the saving session if enabled
            if self.rate_export_max > 0:
                self.load_saving_slot(self.octopus_saving_slots, export=True, rate_replicate=self.rate_export_replicated)
            self.rate_export = self.basic_rates(self.get_arg("rates_export_override", [], indirect=False), "rates_export_override", self.rate_export, self.rate_export_replicated)
            self.rate_export = self.apply_manual_rates(self.rate_export, self.manual_export_rates, is_import=False, rate_replicate=self.rate_export_replicated)
            self.rate_scan_export(self.rate_export, print=True)
        else:
            self.log("Warning: No export rate data provided")
            self.record_status(message="Error: No export rate data provided", had_errors=True)

        # Set rate thresholds
        if self.rate_import or self.rate_export:
            self.set_rate_thresholds()

        # Find discharging windows
        if self.rate_export:
            self.high_export_rates, lowest, highest = self.rate_scan_window(self.rate_export, 5, self.rate_export_cost_threshold, True, alt_rates=self.rate_import)
            # Update threshold automatically
            self.log("High export rate found rates in range {} to {}".format(lowest, highest))
            if self.rate_high_threshold == 0 and lowest <= self.rate_export_max:
                self.rate_export_cost_threshold = lowest

        # Find charging windows
        if self.rate_import:
            # Find charging window
            self.low_rates, lowest, highest = self.rate_scan_window(self.rate_import, 5, self.rate_import_cost_threshold, False, alt_rates=self.rate_export)
            self.log("Low Import rate found rates in range {} to {}".format(lowest, highest))
            # Update threshold automatically
            if self.rate_low_threshold == 0 and highest >= self.rate_min:
                self.rate_import_cost_threshold = highest

        # Work out car plan?
        for car_n in range(self.num_cars):
            if self.octopus_intelligent_charging and car_n == 0:
                if self.car_charging_planned[car_n]:
                    self.log("Car 0 on Octopus Intelligent, active plan for charge")
                else:
                    self.log("Car 0 on Octopus Intelligent, no active plan")
            elif self.car_charging_planned[car_n] or self.car_charging_now[car_n]:
                self.log(
                    "Car {} plan charging from {} to {} with slots {} from soc {} to {} ready by {}".format(
                        car_n,
                        self.car_charging_soc[car_n],
                        self.car_charging_limit[car_n],
                        self.low_rates,
                        self.car_charging_soc[car_n],
                        self.car_charging_limit[car_n],
                        self.car_charging_plan_time[car_n],
                    )
                )
                self.car_charging_slots[car_n] = self.plan_car_charging(car_n, self.low_rates)
            else:
                self.log("Car {} charging is not planned as it is not plugged in".format(car_n))

            # Log the charging plan
            if self.car_charging_slots[car_n]:
                self.log("Car {} charging plan is: {}".format(car_n, self.car_charging_slots[car_n]))

            if self.car_charging_planned[car_n] and self.car_charging_exclusive[car_n]:
                self.log("Car {} charging is exclusive, will not plan other cars".format(car_n))
                break

        # Publish the car plan
        self.publish_car_plan()

        # Work out iboost plan
        if self.iboost_enable and (((not self.iboost_solar) and (not self.iboost_charging)) or self.iboost_smart):
            self.iboost_plan = self.plan_iboost_smart()
            self.log(
                "IBoost iboost_solar {} rate threshold import {} rate threshold  export {} iboost_gas {} iboost_gas_export {} iboost_smart {} min_length {} plan is: {}".format(
                    self.iboost_solar,
                    self.iboost_rate_threshold,
                    self.iboost_rate_threshold_export,
                    self.iboost_gas,
                    self.iboost_gas_export,
                    self.iboost_smart,
                    self.iboost_smart_min_length,
                    self.iboost_plan,
                )
            )

        # Work out cost today
        if self.import_today:
            self.cost_today_sofar, self.carbon_today_sofar = self.today_cost(self.import_today, self.export_today, self.car_charging_energy, self.load_minutes)

        # Fetch PV forecast if enabled, today must be enabled, other days are optional
        self.pv_forecast_minute, self.pv_forecast_minute10 = self.fetch_pv_forecast()

        # Apply modal filter to historical data
        if self.load_minutes and not self.load_forecast_only:
            self.previous_days_modal_filter(self.load_minutes)
            self.log("Historical days now {} weight {}".format(self.days_previous, self.days_previous_weight))

        # Load today vs actual
        if self.load_minutes:
            self.load_inday_adjustment = self.load_today_comparison(self.load_minutes, self.load_forecast, self.car_charging_energy, self.import_today, self.minutes_now)
        else:
            self.load_inday_adjustment = 1.0

    def predict_battery_temperature(self, battery_temperature_history, step):
        """
        Given historical battery temperature data, predict the future temperature

        For now a fairly simple look back over 24 hours is used, can be improved with outdoor temperature later
        """

        predicted_temp = {}
        current_temp = battery_temperature_history.get(0, 20)
        predict_timestamps = {}

        for minute in range(0, self.forecast_minutes, step):
            timestamp = self.now_utc + timedelta(minutes=minute)
            timestamp_str = timestamp.strftime(TIME_FORMAT)
            predicted_temp[minute] = dp2(current_temp)
            predict_timestamps[timestamp_str] = dp2(current_temp)

            # Look at 30 minute change 24 hours ago to predict the up/down trend
            minute_previous = (24 * 60 - minute) % (24 * 60)
            change = battery_temperature_history.get(minute_previous, 20) - battery_temperature_history.get(minute_previous + step, 20)
            current_temp += change
            current_temp = max(min(current_temp, 30), -20)

        self.dashboard_item(
            self.prefix + ".battery_temperature",
            state=dp2(battery_temperature_history.get(0, 20)),
            attributes={
                "results": self.filtered_times(predict_timestamps),
                "temperature_h1": battery_temperature_history.get(60, 20),
                "temperature_h2": battery_temperature_history.get(60 * 2, 20),
                "temperature_h8": battery_temperature_history.get(60 * 8, 20),
                "friendly_name": "Battery temperature",
                "state_class": "measurement",
                "unit_of_measurement": "°C",
                "icon": "mdi:temperature-celsius",
            },
        )
        return predicted_temp

    def rate_replicate(self, rates, rate_io={}, is_import=True, is_gas=False):
        """
        We don't get enough hours of data for Octopus, so lets assume it repeats until told others
        """
        minute = -24 * 60
        rate_last = 0
        adjusted_rates = {}
        replicated_rates = {}

        # Add 48 extra hours to make sure the whole cycle repeats another day
        while minute < (self.forecast_minutes + 48 * 60):
            if minute not in rates:
                adjust_type = "copy"
                # Take 24-hours previous if missing rate
                if (minute - 24 * 60) in rates:
                    minute_mod = minute - 24 * 60
                elif (minute >= 24 * 60) and ((minute - 24 * 60) in rates):
                    minute_mod = minute - 24 * 60
                else:
                    minute_mod = minute % (24 * 60)

                if (minute_mod in rate_io) and rate_io[minute_mod]:
                    # Dont replicate Intelligent rates into the next day as it will be different
                    rate_offset = self.rate_max
                elif minute_mod in rates:
                    rate_offset = rates[minute_mod]
                else:
                    # Missing rate within 24 hours - fill with dummy last rate
                    rate_offset = rate_last

                # Only offset once not every day
                futurerate_adjust_import = self.get_arg("futurerate_adjust_import", False)
                futurerate_adjust_export = self.get_arg("futurerate_adjust_export", False)
                if minute_mod not in adjusted_rates:
                    if is_import and futurerate_adjust_import and (minute in self.future_energy_rates_import) and (minute_mod in self.future_energy_rates_import):
                        rate_offset = self.future_energy_rates_import[minute]
                        adjust_type = "future"
                    elif (not is_import) and (not is_gas) and futurerate_adjust_export and (minute in self.future_energy_rates_export) and (minute_mod in self.future_energy_rates_export):
                        rate_offset = max(self.future_energy_rates_export[minute], 0)
                        adjust_type = "future"
                    elif is_import:
                        rate_offset = rate_offset + self.metric_future_rate_offset_import
                        if self.metric_future_rate_offset_import:
                            adjust_type = "offset"
                    elif (not is_import) and (not is_gas):
                        rate_offset = max(rate_offset + self.metric_future_rate_offset_export, 0)
                        if self.metric_future_rate_offset_export:
                            adjust_type = "offset"

                    adjusted_rates[minute] = True

                rates[minute] = rate_offset
                replicated_rates[minute] = adjust_type
            else:
                rate_last = rates[minute]
            minute += 1
        return rates, replicated_rates

    def find_charge_window(self, rates, minute, threshold_rate, find_high, alt_rates={}):
        """
        Find the charging windows based on the low rate threshold (percent below average)
        """
        rate_low_start = -1
        rate_low_end = -1
        rate_low_average = 0
        rate_low_rate = 0
        rate_low_count = 0
        alternate_rate_boundary = False
        alt_rate_last = None
        alt_rate_last = None

        # Work out alternate rate threshold
        alt_rate_max = max(alt_rates.values()) if alt_rates else 0
        alt_rate_min = min(alt_rates.values()) if alt_rates else 0
        alt_rate_threshold = abs(alt_rate_max - alt_rate_min) * 0.1

        stop_at = self.forecast_minutes + self.minutes_now + 12 * 60
        # Scan for lower rate start and end
        while minute < stop_at and not (minute >= (self.forecast_minutes + self.minutes_now) and (rate_low_start < 0)):
            alt_rate = alt_rates.get(minute, None)
            if (alt_rate is not None) and (alt_rate_last is not None) and (abs(alt_rate - alt_rate_last) >= alt_rate_threshold):
                # Create alternate rate boundary if the rate is different
                alternate_rate_boundary = True
            if minute in rates:
                rate = rates[minute]
                if ((not find_high) and (rate <= threshold_rate)) or (find_high and (rate >= threshold_rate) and (rate > 0)) or (minute in self.manual_all_times) or (rate_low_start in self.manual_all_times):
                    rate_diff = abs(rate - rate_low_rate)
                    if (rate_low_start >= 0) and rate_diff > self.combine_rate_threshold:
                        # Refuse mixed rates that are different by more than threshold
                        rate_low_end = minute
                        break
                    if find_high and (not self.combine_export_slots) and (rate_low_start >= 0) and ((minute - rate_low_start) >= self.export_slot_split):
                        # If combine is disabled, for export slots make them all N minutes so we can select some not all
                        rate_low_end = minute
                        break
                    if (not find_high) and (not self.combine_charge_slots) and (rate_low_start >= 0) and ((minute - rate_low_start) >= self.charge_slot_split):
                        # If combine is disabled, for import slots make them all N minutes so we can select some not all
                        rate_low_end = minute
                        break
                    if (rate_low_start in self.manual_all_times or minute in self.manual_all_times) and (rate_low_start >= 0) and ((minute - rate_low_start) >= 30):
                        # Manual slot
                        rate_low_end = minute
                        break
                    if find_high and (rate_low_start >= 0) and (((minute - rate_low_start) >= 60 * 24) or (((minute - rate_low_start) >= 30) and alternate_rate_boundary)):
                        # Export slot can never be bigger than 4 hours
                        rate_low_end = minute
                        break
                    if rate_low_start < 0:
                        rate_low_start = minute
                        rate_low_end = stop_at
                        rate_low_count = 1
                        rate_low_average = rate
                        rate_low_rate = rate
                    elif rate_low_end > minute:
                        rate_low_average += rate
                        rate_low_count += 1
                else:
                    if rate_low_start >= 0:
                        rate_low_end = minute
                        break
            else:
                if rate_low_start >= 0 and rate_low_end >= minute:
                    rate_low_end = minute
                break
            minute += 5
            alt_rate_last = alt_rate

        if rate_low_count:
            rate_low_average = dp2(rate_low_average / rate_low_count)
        return rate_low_start, rate_low_end, rate_low_average

    def apply_manual_rates(self, rates, manual_items, is_import=True, rate_replicate={}):
        """
        Apply manual rates to the rates dictionary
        """
        if not manual_items:
            return rates

        for minute in manual_items:
            rate = manual_items[minute]
            try:
                rate = float(rate)
            except (ValueError, TypeError):
                self.log("Warn: Bad rate {} provided in manual rates".format(rate))
                self.record_status("Bad rate {} provided in manual rates".format(rate), had_errors=True)
                continue
            rates[minute] = rate
            rate_replicate[minute] = "manual"

        return rates

    def basic_rates(self, info, rtype, prev=None, rate_replicate={}):
        """
        Work out the energy rates based on user supplied time periods
        works on a 24-hour period only and then gets replicated later for future days
        """
        rates = {}

        if prev:
            rates = prev.copy()
            max_minute = max(rates) + 1
        else:
            # Set to zero
            for minute in range(24 * 60):
                rates[minute] = 0
            max_minute = 48 * 60

        manual_items = self.get_manual_api(rtype)
        if manual_items:
            self.log("Basic rate API override items for {} are {}".format(rtype, manual_items))

        midnight = time_string_to_stamp("00:00:00")
        for this_rate in info + manual_items:
            if this_rate and isinstance(this_rate, dict):
                start_str = this_rate.get("start", "00:00:00")
                start_str = self.resolve_arg("start", start_str, "00:00:00")
                end_str = this_rate.get("end", "00:00:00")
                end_str = self.resolve_arg("end", end_str, "00:00:00")
                load_scaling = this_rate.get("load_scaling", None)

                if start_str.count(":") < 2:
                    start_str += ":00"
                if end_str.count(":") < 2:
                    end_str += ":00"

                try:
                    start = time_string_to_stamp(start_str)
                except (ValueError, TypeError):
                    self.log("Warn: Bad start time {} provided in energy rates".format(start_str))
                    self.record_status("Bad start time {} provided in energy rates".format(start_str), had_errors=True)
                    continue

                try:
                    end = time_string_to_stamp(end_str)
                except (ValueError, TypeError):
                    self.log("Warn: Bad end time {} provided in energy rates".format(end_str))
                    self.record_status("Bad end time {} provided in energy rates".format(end_str), had_errors=True)
                    continue

                date = None
                if "date" in this_rate:
                    date_str = self.resolve_arg("date", this_rate["date"])
                    try:
                        date = datetime.strptime(date_str, "%Y-%m-%d")
                    except (ValueError, TypeError):
                        self.log("Warn: Bad date {} provided in energy rates".format(this_rate["date"]))
                        self.record_status("Bad date {} provided in energy rates".format(this_rate["date"]), had_errors=True)
                        continue
                day_of_week = []
                if "day_of_week" in this_rate:
                    day = str(this_rate["day_of_week"])
                    days = day.split(",")
                    for day in days:
                        try:
                            day = int(day)
                        except (ValueError, TypeError):
                            self.log("Warn: Bad day_of_week {} provided in energy rates, should be 0-6".format(day_of_week))
                            self.record_status("Bad day_of_week {} provided in energy rates, should be 0-6".format(day_of_week), had_errors=True)
                            continue
                        if day < 1 or day > 7:
                            self.log("Warn: Bad day_of_week {} provided in energy rates, should be 0-6".format(day))
                            self.record_status("Bad day_of_week {} provided in energy rates, should be 0-6".format(day), had_errors=True)
                            continue
                        # Store as Python day of week
                        day_of_week.append(day - 1)

                # Support increment to existing rates (for override)
                if "rate" in this_rate:
                    rate = this_rate.get("rate", 0.0)
                    rate_increment = False
                elif "rate_increment" in this_rate:
                    rate = this_rate.get("rate_increment", 0.0)
                    rate_increment = True
                else:
                    rate = 0
                    rate_increment = True

                # Resolve any sensor links
                if isinstance(rate, str) and rate[0].isalpha():
                    rate = self.resolve_arg("rate", rate, 0.0)
                if isinstance(load_scaling, str) and load_scaling[0].isalpha():
                    load_scaling = self.resolve_arg("load_scaling", load_scaling, 1.0)

                # Ensure the end result is a float
                try:
                    rate = float(rate)
                except (ValueError, TypeError):
                    self.log("Warn: Bad rate {} provided in energy rates".format(rate))
                    self.record_status("Bad rate {} provided in energy rates".format(rate), had_errors=True)
                    continue

                if load_scaling is not None:
                    try:
                        load_scaling = float(load_scaling)
                    except (ValueError, TypeError):
                        self.log("Warn: Bad load_scaling {} provided in energy rates".format(load_scaling))
                        self.record_status("Warn: Bad load_scaling {} provided in energy rates".format(load_scaling), had_errors=True)
                        continue

                # Time in minutes
                start_minutes = max(minutes_to_time(start, midnight), 0)
                end_minutes = min(minutes_to_time(end, midnight), 24 * 60 - 1)

                # Make end > start
                if end_minutes <= start_minutes:
                    end_minutes += 24 * 60

                # Adjust for date if specified
                if date:
                    delta_minutes = minutes_to_time(date, self.midnight)
                    start_minutes += delta_minutes
                    end_minutes += delta_minutes

                self.log("Adding rate {}: {} => {} to {} @ {} date {} day_of_week {} increment {}".format(rtype, this_rate, self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), rate, date, day_of_week, rate_increment))

                day_of_week_midnight = self.midnight.weekday()

                # Store rates against range
                if end_minutes >= (-48 * 60) and start_minutes < max_minute:
                    for minute in range(start_minutes, end_minutes):
                        minute_mod = minute % max_minute
                        if (not date) or (minute >= (-24 * 60) and minute < max_minute):
                            minute_index = minute_mod - 24 * 60
                            # For incremental adjustments we have to loop over 24-hour periods
                            while minute_index < max_minute:
                                if not date or (minute_index >= start_minutes and minute_index < end_minutes):
                                    current_day_of_week = (day_of_week_midnight + int(minute_index / (24 * 60))) % 7
                                    if not day_of_week or (current_day_of_week in day_of_week):
                                        if rate_increment:
                                            rates[minute_index] = rates.get(minute_index, 0.0) + rate
                                            rate_replicate[minute_index] = "increment"
                                        else:
                                            rates[minute_index] = rate
                                            rate_replicate[minute_index] = "user"
                                        if load_scaling is not None:
                                            self.load_scaling_dynamic[minute_index] = load_scaling
                                        if date:
                                            break
                                minute_index += 24 * 60
                            if not date and not prev:
                                rates[minute_mod + max_minute] = rate
                                if load_scaling is not None:
                                    self.load_scaling_dynamic[minute_mod + max_minute] = load_scaling
            else:
                self.log("Warn: Bad rate data provided in energy rates type {} {}".format(rtype, this_rate))

        return rates

    def rate_scan_export(self, rates, print=True):
        """
        Scan the rates and work out min/max
        """

        self.rate_export_min, self.rate_export_max, self.rate_export_average, self.rate_export_min_minute, self.rate_export_max_minute = self.rate_minmax(rates)
        if print:
            self.log("Export rates min {} max {} average {}".format(self.rate_export_min, self.rate_export_max, self.rate_export_average))

    def rate_minmax(self, rates):
        """
        Work out min and max rates
        """
        rate_min = 99999
        rate_min_minute = 0
        rate_max_minute = 0
        rate_max = 0
        rate_average = 0
        rate_n = 0
        rate = 0

        # Scan rates and find min/max/average
        if rates:
            rate = rates.get(self.minutes_now, 0)
            for minute in range(self.minutes_now, self.forecast_minutes + self.minutes_now):
                if minute in rates:
                    rate = rates[minute]
                    if rate > rate_max:
                        rate_max = rate
                        rate_max_minute = minute
                    if rate < rate_min:
                        rate_min = rate
                        rate_min_minute = minute
                    rate_average += rate
                    rate_n += 1
                minute += 1
        if rate_n:
            rate_average /= rate_n

        return dp2(rate_min), dp2(rate_max), dp2(rate_average), rate_min_minute, rate_max_minute

    def rate_min_forward_calc(self, rates):
        """
        Work out lowest rate from time forwards
        """
        rate_array = []
        rate_min_forward = {}
        rate = self.rate_min

        for minute in range(self.forecast_minutes + self.minutes_now + 48 * 60):
            if minute in rates:
                rate = rates[minute]
            rate_array.append(rate)

        # Work out the min rate going forward
        for minute in range(self.minutes_now, self.forecast_minutes + 24 * 60 + self.minutes_now):
            rate_min_forward[minute] = min(rate_array[minute:])

        self.log("Rate min forward looking: now {} at end of forecast {}".format(dp2(rate_min_forward[self.minutes_now]), dp2(rate_min_forward[self.forecast_minutes])))

        return rate_min_forward

    def rate_scan_window(self, rates, rate_low_min_window, threshold_rate, find_high, return_raw=False, alt_rates={}):
        """
        Scan for the next high/low rate window
        """
        minute = 0
        found_rates = []
        lowest = 99
        highest = -99
        upcoming_period = self.minutes_now + 4 * 60

        while True:
            rate_low_start, rate_low_end, rate_low_average = self.find_charge_window(rates, minute, threshold_rate, find_high, alt_rates=alt_rates)
            window = {}
            window["start"] = rate_low_start
            window["end"] = rate_low_end
            window["average"] = dp2(rate_low_average)

            if rate_low_start >= 0:
                if (return_raw or (rate_low_end > self.minutes_now)) and (rate_low_end - rate_low_start) >= rate_low_min_window:
                    if rate_low_average < lowest:
                        lowest = rate_low_average
                    if rate_low_average > highest:
                        highest = rate_low_average

                    found_rates.append(window)
                minute = rate_low_end
            else:
                break

        return found_rates, lowest, highest

    def set_rate_thresholds(self):
        """
        Set the high and low rate thresholds
        """

        have_alerts = len(self.alert_active_keep) > 0

        if self.rate_low_threshold > 0:
            self.rate_import_cost_threshold = dp2(self.rate_average * self.rate_low_threshold)
        else:
            # In automatic mode select the only rate or everything but the most expensive
            if (self.rate_max == self.rate_min) or (self.rate_export_max > self.rate_max) or have_alerts or self.num_cars > 0:
                self.rate_import_cost_threshold = self.rate_max + 0.1
            else:
                self.rate_import_cost_threshold = self.rate_max - 0.5

        # Compute the export rate threshold
        if self.rate_high_threshold > 0:
            self.rate_export_cost_threshold = dp2(self.rate_export_average * self.rate_high_threshold)
        else:
            # In automatic mode select the only rate or everything but the most cheapest
            if (self.rate_export_max == self.rate_export_min) or (self.rate_export_min > self.rate_min) or have_alerts:
                self.rate_export_cost_threshold = self.rate_export_min - 0.1
            else:
                self.rate_export_cost_threshold = self.rate_export_min + 0.5

        self.log("Rate thresholds (for charge/export) are import {}p ({}) export {}p ({})".format(self.rate_import_cost_threshold, self.rate_low_threshold, self.rate_export_cost_threshold, self.rate_high_threshold))

    def rate_scan(self, rates, print=True):
        """
        Scan the rates and work out min/max
        """
        self.rate_min, self.rate_max, self.rate_average, self.rate_min_minute, self.rate_max_minute = self.rate_minmax(rates)

        if print:
            # Calculate minimum forward rates only once rate replicate has run (when print is True)
            self.rate_min_forward = self.rate_min_forward_calc(self.rate_import)
            self.log("Import rates min {} max {} average {}".format(self.rate_min, self.rate_max, self.rate_average))

    def rate_scan_gas(self, rates, print=True):
        """
        Scan the gas rates and work out min/max
        """
        self.rate_gas_min, self.rate_gas_max, self.rate_gas_average, self.rate_gas_min_minute, self.rate_gas_max_minute = self.rate_minmax(rates)

        if print:
            self.log("Gas rates min {} max {} average {}".format(self.rate_gas_min, self.rate_gas_max, self.rate_gas_average))

    def get_car_charging_planned(self):
        """
        Get the car attributes
        """
        self.car_charging_planned = [False for c in range(self.num_cars)]
        self.car_charging_now = [False for c in range(self.num_cars)]
        self.car_charging_plan_smart = [False for c in range(self.num_cars)]
        self.car_charging_plan_max_price = [0 for c in range(self.num_cars)]
        self.car_charging_plan_time = ["07:00:00" for c in range(self.num_cars)]
        self.car_charging_battery_size = [100.0 for c in range(self.num_cars)]
        self.car_charging_limit = [100.0 for c in range(self.num_cars)]
        self.car_charging_rate = [7.4 for c in range(max(self.num_cars, 1))]
        self.car_charging_slots = [[] for c in range(self.num_cars)]
        self.car_charging_exclusive = [False for c in range(self.num_cars)]

        self.car_charging_planned_response = self.get_arg("car_charging_planned_response", ["yes", "on", "enable", "true"])
        self.car_charging_now_response = self.get_arg("car_charging_now_response", ["yes", "on", "enable", "true"])
        self.car_charging_from_battery = self.get_arg("car_charging_from_battery")

        # Car charging planned sensor
        for car_n in range(self.num_cars):
            # Get car N planned status
            planned = self.get_arg("car_charging_planned", "no", index=car_n)
            if isinstance(planned, str):
                if planned.lower() in self.car_charging_planned_response:
                    planned = True
                else:
                    planned = False
            elif not isinstance(planned, bool):
                planned = False
            self.car_charging_planned[car_n] = planned

            # Car is charging now sensor
            charging_now = self.get_arg("car_charging_now", "no", index=car_n)
            if isinstance(charging_now, str):
                if charging_now.lower() in self.car_charging_now_response:
                    charging_now = True
                else:
                    charging_now = False
            elif not isinstance(charging_now, bool):
                charging_now = False
            self.car_charging_now[car_n] = charging_now

            # Other car related configuration
            self.car_charging_plan_smart[car_n] = self.get_arg("car_charging_plan_smart", False)
            self.car_charging_plan_max_price[car_n] = self.get_arg("car_charging_plan_max_price", 0.0)
            self.car_charging_plan_time[car_n] = self.get_arg("car_charging_plan_time", "07:00:00")
            self.car_charging_battery_size[car_n] = float(self.get_arg("car_charging_battery_size", 100.0, index=car_n))
            car_postfix = "" if car_n == 0 else "_" + str(car_n)
            self.car_charging_rate[car_n] = float(self.get_arg("car_charging_rate" + car_postfix))
            self.car_charging_limit[car_n] = dp3((float(self.get_arg("car_charging_limit", 100.0, index=car_n)) * self.car_charging_battery_size[car_n]) / 100.0)
            self.car_charging_exclusive[car_n] = self.get_arg("car_charging_exclusive", False, index=car_n)

        if self.num_cars > 0:
            self.log(
                "Cars {} charging from battery {} planned {}, charging_now {} smart {}, max_price {}, plan_time {}, battery size {}, limit {}, rate {}, exclusive {}".format(
                    self.num_cars,
                    self.car_charging_from_battery,
                    self.car_charging_planned,
                    self.car_charging_now,
                    self.car_charging_plan_smart,
                    self.car_charging_plan_max_price,
                    self.car_charging_plan_time,
                    self.car_charging_battery_size,
                    self.car_charging_limit,
                    self.car_charging_rate,
                    self.car_charging_exclusive,
                )
            )

    def fetch_extra_load_forecast(self, now_utc):
        """
        Fetch extra load forecast, this is future load data
        """
        load_forecast_final = {}
        load_forecast_array = []

        if "load_forecast" in self.args:
            entity_ids = self.get_arg("load_forecast", indirect=False)
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]

            for entity_id in entity_ids:
                if not entity_id:
                    self.log("Warn: Unable to fetch load forecast data, check your setting of load_forecast")
                    continue

                attribute = None
                if "$" in entity_id:
                    entity_id, attribute = entity_id.split("$")
                try:
                    self.log("Loading extra load forecast from {} attribute {}".format(entity_id, attribute))
                    data = self.get_state_wrapper(entity_id=entity_id, attribute=attribute)
                except (ValueError, TypeError) as e:
                    self.log("Error: Unable to fetch load forecast data from sensor {} exception {}".format(entity_id, e))
                    data = None

                # Convert format from dict to array
                if isinstance(data, dict):
                    data_array = []
                    for key, value in data.items():
                        data_array.append({"energy": value, "last_updated": key})
                    data = data_array

                # Load data
                load_forecast = self.minute_data(
                    data,
                    self.forecast_days + 1,
                    self.midnight_utc,
                    "energy",
                    "last_updated",
                    backwards=False,
                    clean_increment=False,
                    smoothing=True,
                    divide_by=1.0,
                    scale=self.load_scaling,
                    required_unit="kWh",
                )

                if load_forecast:
                    self.log("Loaded load forecast from {} load from midnight {} to now {} to midnight {}".format(entity_id, load_forecast.get(0, 0), load_forecast.get(self.minutes_now, 0), load_forecast.get(24 * 60, 0)))
                else:
                    self.log("Warn: Unable to load load forecast from {}".format(entity_id))

                load_forecast_array.append(load_forecast)

        # Add all the load forecasts together
        for load in load_forecast_array:
            for minute, value in load.items():
                if minute in load_forecast_final:
                    load_forecast_final[minute] += value
                else:
                    load_forecast_final[minute] = value
        return load_forecast_final, load_forecast_array

    def fetch_carbon_intensity(self, entity_id):
        """
        Fetch the carbon intensity from the sensor
        Returns a dictionary with the carbon intensity data for the next 24 hours
        And a secondary dictionary with the carbon intensity data for the last 24 hours

        :param entity_id: The entity_id of the sensor
        """
        data_all = []
        carbon_data = {}
        carbon_history = {}

        if entity_id:
            self.log("Fetching carbon intensity data from {}".format(entity_id))
            data_all = self.get_state_wrapper(entity_id=entity_id, attribute="forecast")
            if data_all:
                carbon_data = self.minute_data(data_all, self.forecast_days, self.now_utc, "intensity", "from", backwards=False, to_key="to")

        entity_id = self.prefix + ".carbon_now"
        state = self.get_state_wrapper(entity_id=entity_id)
        if state is not None:
            try:
                carbon_history = self.minute_data_import_export(self.now_utc, entity_id, required_unit="g/kWh", increment=False, smoothing=False)
            except (ValueError, TypeError):
                self.log("Warn: No carbon intensity history in sensor {}".format(entity_id))
        else:
            self.log("Warn: No carbon intensity history in sensor {}".format(entity_id))

        return carbon_data, carbon_history

    def fetch_config_options(self):
        """
        Fetch all the configuration options
        """

        self.debug_enable = self.get_arg("debug_enable")
        self.plan_debug = self.get_arg("plan_debug")
        self.previous_status = self.get_state_wrapper(self.prefix + ".status")
        forecast_hours = max(self.get_arg("forecast_hours", 48), 24)

        self.num_cars = self.get_arg("num_cars", 1)
        self.calculate_plan_every = max(self.get_arg("calculate_plan_every"), 5)

        self.log("Configuration: forecast_hours {} num_cars {} debug enable is {} calculate_plan_every {}".format(forecast_hours, self.num_cars, self.debug_enable, self.calculate_plan_every))

        # Days previous
        self.holiday_days_left = self.get_arg("holiday_days_left")
        self.load_forecast_only = self.get_arg("load_forecast_only", False)

        self.days_previous = self.get_arg("days_previous", [7])
        self.days_previous_weight = self.get_arg("days_previous_weight", [1 for i in range(len(self.days_previous))])
        if len(self.days_previous) > len(self.days_previous_weight):
            # Extend weights with 1 if required
            self.days_previous_weight += [1 for i in range(len(self.days_previous) - len(self.days_previous_weight))]
        if self.holiday_days_left > 0:
            self.days_previous = [1]
            self.log("Holiday mode is active, {} days remaining, setting days previous to 1".format(self.holiday_days_left))
        self.max_days_previous = max(self.days_previous) + 1

        self.forecast_days = int((forecast_hours + 23) / 24)
        self.forecast_minutes = forecast_hours * 60
        self.forecast_plan_hours = max(min(self.get_arg("forecast_plan_hours"), forecast_hours), 8)
        self.inverter_clock_skew_start = self.get_arg("inverter_clock_skew_start", 0)
        self.inverter_clock_skew_end = self.get_arg("inverter_clock_skew_end", 0)
        self.inverter_clock_skew_discharge_start = self.get_arg("inverter_clock_skew_discharge_start", 0)
        self.inverter_clock_skew_discharge_end = self.get_arg("inverter_clock_skew_discharge_end", 0)
        self.inverter_can_charge_during_export = self.get_arg("inverter_can_charge_during_export", True)

        # Log clock skew
        if self.inverter_clock_skew_start != 0 or self.inverter_clock_skew_end != 0:
            self.log("Inverter clock skew start {} end {} applied".format(self.inverter_clock_skew_start, self.inverter_clock_skew_end))
        if self.inverter_clock_skew_discharge_start != 0 or self.inverter_clock_skew_discharge_end != 0:
            self.log("Inverter clock skew discharge start {} end {} applied".format(self.inverter_clock_skew_discharge_start, self.inverter_clock_skew_discharge_end))

        # Metric config
        self.metric_min_improvement = self.get_arg("metric_min_improvement")
        self.metric_min_improvement_export = self.get_arg("metric_min_improvement_export")
        self.metric_min_improvement_swap = self.get_arg("metric_min_improvement_swap")
        self.metric_min_improvement_plan = self.get_arg("metric_min_improvement_plan")
        self.metric_min_improvement_export_freeze = self.get_arg("metric_min_improvement_export_freeze")
        self.metric_battery_cycle = self.get_arg("metric_battery_cycle")
        self.metric_self_sufficiency = self.get_arg("metric_self_sufficiency")
        self.metric_future_rate_offset_import = self.get_arg("metric_future_rate_offset_import")
        self.metric_future_rate_offset_export = self.get_arg("metric_future_rate_offset_export")
        self.metric_inday_adjust_damping = self.get_arg("metric_inday_adjust_damping")
        self.metric_pv_calibration_enable = self.get_arg("metric_pv_calibration_enable")
        self.rate_low_threshold = self.get_arg("rate_low_threshold")
        self.rate_high_threshold = self.get_arg("rate_high_threshold")
        self.inverter_soc_reset = self.get_arg("inverter_soc_reset")

        self.metric_battery_value_scaling = self.get_arg("metric_battery_value_scaling")
        self.notify_devices = self.get_arg("notify_devices", ["notify"])
        self.pv_scaling = self.get_arg("pv_scaling")
        self.pv_metric10_weight = self.get_arg("pv_metric10_weight")
        self.load_scaling = self.get_arg("load_scaling")
        self.load_scaling10 = self.get_arg("load_scaling10")
        self.load_scaling_saving = self.get_arg("load_scaling_saving")
        self.load_scaling_free = self.get_arg("load_scaling_free")
        self.battery_rate_max_scaling = self.get_arg("battery_rate_max_scaling")
        self.battery_rate_max_scaling_discharge = self.get_arg("battery_rate_max_scaling_discharge")

        self.best_soc_step = 0.25
        self.metric_cloud_enable = self.get_arg("metric_cloud_enable")
        self.metric_load_divergence_enable = self.get_arg("metric_load_divergence_enable")

        # Battery charging options
        self.battery_capacity_nominal = self.get_arg("battery_capacity_nominal")
        self.battery_loss = 1.0 - self.get_arg("battery_loss")
        self.battery_loss_discharge = 1.0 - self.get_arg("battery_loss_discharge")
        self.inverter_loss = 1.0 - self.get_arg("inverter_loss")
        self.inverter_hybrid = self.get_arg("inverter_hybrid")
        self.base_load = self.get_arg("base_load", 0) / 1000.0

        # Charge curve
        if self.args.get("battery_charge_power_curve", "") == "auto":
            self.battery_charge_power_curve_auto = True
        else:
            self.battery_charge_power_curve_auto = False
            self.battery_charge_power_curve = self.args.get("battery_charge_power_curve", {})
            # Check power curve is a dictionary
            if not isinstance(self.battery_charge_power_curve, dict):
                self.battery_charge_power_curve = {}
                self.log("Warn: battery_charge_power_curve is incorrectly configured - ignoring")
                self.record_status("battery_charge_power_curve is incorrectly configured - ignoring", had_errors=True)

        # Discharge curve
        if self.args.get("battery_discharge_power_curve", "") == "auto":
            self.battery_discharge_power_curve_auto = True
        else:
            self.battery_discharge_power_curve_auto = False
            self.battery_discharge_power_curve = self.args.get("battery_discharge_power_curve", {})
            # Check power curve is a dictionary
            if not isinstance(self.battery_discharge_power_curve, dict):
                self.battery_discharge_power_curve = {}
                self.log("Warn: battery_discharge_power_curve is incorrectly configured - ignoring")
                self.record_status("battery_discharge_power_curve is incorrectly configured - ignoring", had_errors=True)

        # Temperature curve charge
        self.battery_temperature_charge_curve = self.args.get("battery_temperature_charge_curve", {})
        if not isinstance(self.battery_temperature_charge_curve, dict):
            self.log("Data is {}".format(self.battery_temperature_charge_curve))
            self.battery_temperature_charge_curve = {}
            self.log("Warn: battery_temperature_charge_curve is incorrectly configured - ignoring")
            self.record_status("battery_temperature_charge_curve is incorrectly configured - ignoring", had_errors=True)

        # Temperature curve discharge
        self.battery_temperature_discharge_curve = self.args.get("battery_temperature_discharge_curve", {})
        if not isinstance(self.battery_temperature_discharge_curve, dict):
            self.battery_temperature_discharge_curve = {}
            self.log("Warn: battery_temperature_discharge_curve is incorrectly configured - ignoring")
            self.record_status("battery_temperature_discharge_curve is incorrectly configured - ignoring", had_errors=True)

        self.import_export_scaling = self.get_arg("import_export_scaling", 1.0)
        self.best_soc_margin = 0.0
        self.best_soc_min = self.get_arg("best_soc_min")
        self.best_soc_max = self.get_arg("best_soc_max")
        self.best_soc_keep = self.get_arg("best_soc_keep")
        self.best_soc_keep_weight = self.get_arg("best_soc_keep_weight")
        self.set_soc_minutes = 30
        self.set_window_minutes = 30
        self.inverter_set_charge_before = self.get_arg("inverter_set_charge_before")
        if not self.inverter_set_charge_before:
            self.set_soc_minutes = 0
            self.set_window_minutes = 0
        self.octopus_intelligent_charging = self.get_arg("octopus_intelligent_charging")
        self.octopus_intelligent_ignore_unplugged = self.get_arg("octopus_intelligent_ignore_unplugged")
        self.octopus_intelligent_consider_full = self.get_arg("octopus_intelligent_consider_full")
        self.get_car_charging_planned()
        self.load_inday_adjustment = 1.0

        self.combine_rate_threshold = self.get_arg("combine_rate_threshold")
        self.combine_export_slots = self.get_arg("combine_export_slots")
        self.combine_charge_slots = self.get_arg("combine_charge_slots")
        self.charge_slot_split = 30
        self.export_slot_split = 30
        self.calculate_best = True
        self.set_read_only = self.get_arg("set_read_only")

        # hard wired options, can be configured per inverter later on
        self.set_soc_enable = True
        self.set_reserve_enable = self.get_arg("set_reserve_enable")
        self.set_reserve_hold = True
        self.set_export_freeze = self.get_arg("set_export_freeze")
        self.set_charge_freeze = self.get_arg("set_charge_freeze")
        self.set_charge_low_power = self.get_arg("set_charge_low_power")
        self.set_export_low_power = self.get_arg("set_export_low_power")
        self.charge_low_power_margin = self.get_arg("charge_low_power_margin")
        self.calculate_export_first = True

        self.set_status_notify = self.get_arg("set_status_notify")
        self.set_inverter_notify = self.get_arg("set_inverter_notify")
        self.set_export_freeze_only = self.get_arg("set_export_freeze_only")
        self.set_discharge_during_charge = self.get_arg("set_discharge_during_charge")

        # Mode
        self.predbat_mode = self.get_arg("mode")
        if self.predbat_mode == PREDBAT_MODE_OPTIONS[PREDBAT_MODE_CONTROL_SOC]:
            self.calculate_best_charge = True
            self.calculate_best_export = False
            self.set_charge_window = False
            self.set_export_window = False
            self.set_soc_enable = True
        elif self.predbat_mode == PREDBAT_MODE_OPTIONS[PREDBAT_MODE_CONTROL_CHARGE]:
            self.calculate_best_charge = True
            self.calculate_best_export = False
            self.set_charge_window = True
            self.set_export_window = False
            self.set_soc_enable = True
        elif self.predbat_mode == PREDBAT_MODE_OPTIONS[PREDBAT_MODE_CONTROL_CHARGEDISCHARGE]:
            self.calculate_best_charge = True
            self.calculate_best_export = True
            self.set_charge_window = True
            self.set_export_window = True
            self.set_soc_enable = True
        else:  # PREDBAT_MODE_OPTIONS[PREDBAT_MODE_MONITOR]
            self.calculate_best_charge = False
            self.calculate_best_export = False
            self.set_charge_window = False
            self.set_export_window = False
            self.predbat_mode = PREDBAT_MODE_OPTIONS[PREDBAT_MODE_MONITOR]
            self.set_soc_enable = False
            self.expose_config("mode", self.predbat_mode)

        self.log("Predbat mode is set to {}".format(self.predbat_mode))

        self.calculate_export_oncharge = self.get_arg("calculate_export_oncharge")
        self.calculate_second_pass = self.get_arg("calculate_second_pass")
        self.calculate_inday_adjustment = self.get_arg("calculate_inday_adjustment")
        self.calculate_tweak_plan = self.get_arg("calculate_tweak_plan")
        self.calculate_regions = True
        self.calculate_import_low_export = self.get_arg("calculate_import_low_export")
        self.calculate_export_high_import = self.get_arg("calculate_export_high_import")

        self.balance_inverters_enable = self.get_arg("balance_inverters_enable")
        self.balance_inverters_charge = self.get_arg("balance_inverters_charge")
        self.balance_inverters_discharge = self.get_arg("balance_inverters_discharge")
        self.balance_inverters_crosscharge = self.get_arg("balance_inverters_crosscharge")
        self.balance_inverters_threshold_charge = max(self.get_arg("balance_inverters_threshold_charge"), 1.0)
        self.balance_inverters_threshold_discharge = max(self.get_arg("balance_inverters_threshold_discharge"), 1.0)

        if self.set_read_only:
            self.log("NOTE: Read-only mode is enabled, the inverter controls will not be used!!")

        # Enable load filtering
        self.load_filter_modal = self.get_arg("load_filter_modal")

        # Calculate savings
        self.calculate_savings = True

        # Carbon
        self.carbon_enable = self.get_arg("carbon_enable")
        self.carbon_metric = self.get_arg("carbon_metric")

        # iBoost solar diverter model
        self.iboost_enable = self.get_arg("iboost_enable")
        self.iboost_gas = self.get_arg("iboost_gas")
        self.iboost_gas_export = self.get_arg("iboost_gas_export")
        self.iboost_smart = self.get_arg("iboost_smart")
        self.iboost_smart_min_length = self.get_arg("iboost_smart_min_length")
        self.iboost_on_export = self.get_arg("iboost_on_export")
        self.iboost_prevent_discharge = self.get_arg("iboost_prevent_discharge")
        self.iboost_solar = self.get_arg("iboost_solar")
        self.iboost_solar_excess = self.get_arg("iboost_solar_excess")
        self.iboost_rate_threshold = self.get_arg("iboost_rate_threshold")
        self.iboost_rate_threshold_export = self.get_arg("iboost_rate_threshold_export")
        self.iboost_charging = self.get_arg("iboost_charging")
        self.iboost_gas_scale = self.get_arg("iboost_gas_scale")
        self.iboost_max_energy = self.get_arg("iboost_max_energy")
        self.iboost_max_power = self.get_arg("iboost_max_power") / MINUTE_WATT
        self.iboost_min_power = self.get_arg("iboost_min_power") / MINUTE_WATT
        self.iboost_min_soc = self.get_arg("iboost_min_soc")
        self.iboost_today = self.get_arg("iboost_today")
        self.iboost_value_scaling = self.get_arg("iboost_value_scaling")
        self.iboost_energy_subtract = self.get_arg("iboost_energy_subtract")
        self.iboost_next = self.iboost_today
        self.iboost_running = False
        self.iboost_running_solar = False
        self.iboost_running_full = False
        self.iboost_energy_today = {}
        self.iboost_plan = []

        # Car options
        self.car_charging_hold = self.get_arg("car_charging_hold")
        self.car_charging_manual_soc = self.get_arg("car_charging_manual_soc")
        self.car_charging_threshold = float(self.get_arg("car_charging_threshold")) / 60.0
        self.car_charging_energy_scale = self.get_arg("car_charging_energy_scale")

        # Update list of slot times
        self.manual_charge_times = self.manual_times("manual_charge")
        self.manual_export_times = self.manual_times("manual_export")
        self.manual_freeze_charge_times = self.manual_times("manual_freeze_charge")
        self.manual_freeze_export_times = self.manual_times("manual_freeze_export")
        self.manual_demand_times = self.manual_times("manual_demand")
        self.manual_all_times = self.manual_charge_times + self.manual_export_times + self.manual_demand_times + self.manual_freeze_charge_times + self.manual_freeze_export_times
        self.manual_api = self.api_select_update("manual_api")
        self.manual_import_rates = self.manual_rates("manual_import_rates", default_rate=self.get_arg("manual_import_value"))
        self.manual_export_rates = self.manual_rates("manual_export_rates", default_rate=self.get_arg("manual_export_value"))

        # Update list of config options to save/restore to
        self.update_save_restore_list()

    def load_car_energy(self, now_utc):
        """
        Load previous car charging energy data
        """
        self.car_charging_energy = {}
        if "car_charging_energy" in self.args:
            self.car_charging_energy = self.minute_data_import_export(now_utc, "car_charging_energy", scale=self.car_charging_energy_scale, required_unit="kWh")
        else:
            self.log("Car charging hold {} threshold {}".format(self.car_charging_hold, self.car_charging_threshold * 60.0))
        return self.car_charging_energy
