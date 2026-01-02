# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timedelta
import pytz
from utils import str2time, dp2, dp3, minute_data

from const import TIME_FORMAT

MAX_INCREMENT = 100
PREDICT_STEP = 5


"""
Notes:

A BTU – or British Thermal Unit – is an approximation of the amount of energy required to heat 1lb (one pound) of by 1 degree Fahrenheit, and is roughly equal to 1.055 KJoules

Watts are defined as 1 Watt = 1 Joule per second (1W = 1 J/s) which means that 1 kW = 1000 J/s.

Calculate the kilowatt-hours (kWh) required to heat the water using the following formula: Pt = (4.2 × L × T ) ÷ 3600.
Pt is the power used to heat the water, in kWh. L is the number of liters of water that is being heated and T is the difference in temperature from what you started with, listed in degrees Celsius

So, if the average water temperature across the radiator is 70 degrees C, the Delta T is 50 degrees C. A radiator’s power output is most often expressed in watts.
You can easily convert between Delta 50, 60 and 70, for example: If a radiator has a heat output of 5000 BTU at ΔT=60, to find the heat output at ΔT=50, you simply multiply the BTU by 0.789.
If you have a radiator with a heat output of 5000 BTU at ΔT=60, to find the heat output at ΔT=70, you multiply the BTU by 1.223.
If you have a radiator with a heat output of 5000 BTU at ΔT=50, to find the heat output at ΔT=60, you need to multiply the BTU by 1.264.


FLOMASTA TYPE 22 DOUBLE-PANEL DOUBLE CONVECTOR RADIATOR 600MM X 1200MM WHITE 6998BTU 2051W Delta T50°C - 7.16L
600 x 1000mm DOUBLE-PANEL - 5832BTU 1709W
500 x 900mm DOUBLE-PANEL  - 4519BTU 1324W

https://www.tlc-direct.co.uk/Technical/DataSheets/Quinn_Barlo/BarloPanelOutputsSpecs-Feb-2102.pdf

Delta correction factors (see below)

Steel panel radiators = 11 litres / kW

Key functions:

1. Table of radiators with BTUs (or maybe have size/type and calculate this) and capacity.
2. Work out water content of heating system
3. Ability to set flow temperature either fixed or to sensor
4. User to enter house heat loss figure
   - Need a way to scan past data and calibrate this
   - Look at energy output of heating vs degrees C inside to compute heat loss
5. Take weather data from following days to predict forward outside temperature
6. Predict forward target heating temperature
7. Compute when heating will run, work out water temperature and heat output (and thus energy use)
   - Account for heating efficiency COP
   - Maybe use table of COP vs outside temperature to help predict this for heat pumps?
8. Predict room temperatures
9. Sensor for heating predicted energy to link to Predbat if electric

"""

DELTA_CORRECTION = {75: 1.69, 70: 1.55, 65: 1.41, 60: 1.27, 55: 1.13, 50: 1, 45: 0.87, 40: 0.75, 35: 0.63, 30: 0.51, 25: 0.41, 20: 0.3, 15: 0.21, 10: 0.12, 5: 0.05, 0: 0.00}
GAS_EFFICIENCY = {0: 0.995, 10: 0.995, 20: 0.99, 30: 0.98, 40: 0.95, 50: 0.90, 60: 0.88, 70: 0.87, 80: 0.86, 90: 0.85, 100: 0.84}
HEAT_PUMP_EFFICIENCY = {-20: 2.10, -18: 2.15, -16: 2.2, -14: 2.25, -12: 2.3, -10: 2.4, -8: 2.5, -6: 2.6, -4: 2.7, -2: 2.8, 0: 2.9, 2: 3.1, 4: 3.3, 6: 3.6, 8: 3.8, 10: 3.9, 12: 4.1, 14: 4.3, 16: 4.3, 18: 4.3, 20: 4.3}


class PredHeat:
    """
    The heating prediction class itself
    """

    def fill_table_gaps(self, table):
        """
        Fill gaps correction tables
        """
        if not table:
            return table

        sorted_keys = sorted(table.keys())
        max_key = max(sorted_keys)
        min_key = min(sorted_keys)
        last_key = min_key
        last_value = table[min_key]
        new_table = {}

        for key in range(min_key, max_key + 1):
            if key not in table:
                next_value = last_value
                for next_key in range(key + 1, max_key + 1):
                    if next_key in table:
                        next_value = table[next_key]
                        break
                interpolated = (next_value - last_value) / (next_key - last_key) * (key - last_key) + last_value
                new_table[key] = round(interpolated, 4)
            else:
                last_key = key
                last_value = table[key]
                new_table[key] = last_value
        return new_table

    def __init__(self, base):
        self.base = base
        self.log = base.log
        self.record_status = base.record_status
        self.get_arg = base.get_arg
        self.set_state = base.set_state_wrapper
        self.call_service = base.call_service_wrapper
        self.get_history = base.get_history_wrapper
        self.expose_config = base.expose_config
        self.get_services = base.get_services_wrapper
        self.run_every = base.run_every
        self.heat_pump_efficiency = HEAT_PUMP_EFFICIENCY.copy()
        self.gas_efficiency = GAS_EFFICIENCY.copy()
        self.delta_correction = DELTA_CORRECTION.copy()
        self.weather_compensation = {}
        self.weather_compensation_enabled = False
        self.hysteresis_on = abs(self.get_arg("hysteresis", 0.5, domain="predheat"))
        self.hysteresis_off = abs(self.get_arg("hysteresis_off", 0.1, domain="predheat"))

        # Collect predheat settings
        heat_pump_efficiency = self.get_arg("heat_pump_efficiency", {}, domain="predheat")
        for key, value in heat_pump_efficiency.items():
            self.heat_pump_efficiency[key] = value

        gas_efficiency = self.get_arg("gas_efficiency", {}, domain="predheat")
        for key, value in gas_efficiency.items():
            self.gas_efficiency[key] = value

        delta_correction = self.get_arg("delta_correction", {}, domain="predheat")
        for key, value in delta_correction.items():
            self.delta_correction[key] = value

        weather_compensation = self.get_arg("weather_compensation", {}, domain="predheat")
        if weather_compensation:
            for key, value in weather_compensation.items():
                self.weather_compensation[key] = value
            self.weather_compensation_enabled = True

        # Fill gaps in tables
        self.delta_correction = self.fill_table_gaps(self.delta_correction)
        self.gas_efficiency = self.fill_table_gaps(self.gas_efficiency)
        self.heat_pump_efficiency = self.fill_table_gaps(self.heat_pump_efficiency)
        self.weather_compensation = self.fill_table_gaps(self.weather_compensation)

        self.heat_pump_efficiency_max = max(self.heat_pump_efficiency.values())
        self.log("Predheat: Delta correction {}".format(self.delta_correction))
        self.log("Predheat: Gas boiler efficiency {}".format(self.gas_efficiency))
        self.log("Predheat: Heat pump efficiency {}".format(self.heat_pump_efficiency))
        self.log("Predheat: Heat pump efficiency max {}".format(self.heat_pump_efficiency_max))

        if self.weather_compensation_enabled:
            self.log("Predheat: Weather compensation {}".format(self.weather_compensation))

    def minutes_to_time(self, updated, now):
        """
        Compute the number of minutes between a time (now) and the updated time
        """
        timeday = updated - now
        minutes = int(timeday.seconds / 60) + int(timeday.days * 60 * 24)
        return minutes

    def reset(self):
        self.had_errors = False
        self.prediction_started = False
        self.update_pending = False
        self.prefix = self.get_arg("prefix", "predheat", domain="predheat")
        self.days_previous = [7]
        self.days_previous_weight = [1]
        self.octopus_url_cache = {}

    def get_weather_data(self, now_utc):
        entity_id = self.get_arg("weather", indirect=False, domain="predheat")

        result = self.call_service("weather/get_forecasts", type="hourly", entity_id=entity_id, return_response=True)
        if result:
            data = result.get(entity_id, {}).get("forecast", {})
        else:
            data = {}

        self.temperatures = {}

        if data:
            self.temperatures, _ = minute_data(data, self.forecast_days, now_utc, "temperature", "datetime", backwards=False, smoothing=True, prev_last_updated_time=now_utc, last_state=self.external_temperature[0], max_increment=MAX_INCREMENT)
        else:
            self.log("WARN: Unable to fetch data for {}".format(entity_id))
            self.record_status("Warn - Unable to fetch data from {}".format(entity_id), had_errors=True)

    def minute_data_entity(self, now_utc, key, incrementing=False, smoothing=False, scaling=1.0):
        """
        Download one or more entities of data
        """
        entity_ids = self.get_arg(key, indirect=False, domain="predheat", default=None)
        if not entity_ids:
            return {}, 0
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        data_points = {}
        age_days = None
        total_count = len(entity_ids)
        for entity_id in entity_ids:
            try:
                history = self.get_history(entity_id=entity_id, days=self.max_days_previous)
            except (ValueError, TypeError):
                history = []

            if history and len(history[0]) > 0:
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

            if history and len(history[0]) > 0:
                data_points, _ = minute_data(
                    history[0], self.max_days_previous, now_utc, "state", "last_updated", backwards=True, smoothing=smoothing, scale=scaling / total_count, clean_increment=incrementing, accumulate=data_points, max_increment=MAX_INCREMENT
                )
            else:
                self.log("WARN: Unable to fetch history for {}".format(entity_id))
                self.record_status("Warn - Unable to fetch history from {}".format(entity_id), had_errors=True)

        if age_days is None:
            age_days = 0
        return data_points, age_days

    def get_from_incrementing(self, data, index):
        """
        Get a single value from an incrementing series e.g. kwh today -> kwh this minute
        """
        while index < 0:
            index += 24 * 60
        return data.get(index, 0) - data.get(index + 1, 0)

    def get_from_history(self, data, index):
        """
        Get a single value from a series e.g. temperature now
        """
        while index < 0:
            index += 24 * 60
        return data.get(index, 0)

    def get_historical(self, data, minute, default=0):
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
            use_days = min(days, self.minute_data_age)
            weight = self.days_previous_weight[this_point]
            if use_days > 0:
                full_days = 24 * 60 * (use_days - 1)
                minute_previous = 24 * 60 - minute + full_days
                value = self.get_from_history(data, minute_previous)
                total += value * weight
                total_weight += weight
            this_point += 1

        # Zero data?
        if total_weight == 0:
            return default
        else:
            return total / total_weight

    def run_simulation(self, volume_temp, heating_active, save="best", last_predict_minute=None):
        internal_temp = self.internal_temperature[0]
        external_temp = self.external_temperature[0]
        internal_temp_predict_stamp = {}
        external_temp_predict_stamp = {}
        internal_temp_predict_minute = {}
        target_temp_predict_stamp = {}
        target_temp_predict_minute = {}
        heat_energy = self.heat_energy_today
        heat_energy_predict_minute = {}
        heat_energy_predict_stamp = {}
        heat_to_predict_stamp = {}
        heat_to_predict_temperature = {}
        heating_on = False
        next_volume_temp = volume_temp
        volume_temp_stamp = {}
        WATTS_TO_DEGREES = 1.16
        cost = self.import_today_cost
        cost_stamp = {}
        cost_minute = {}
        energy_today_external = []
        adjustment_points = []

        # Find temperature adjustment points (thermostat turned up)
        adjust_ptr = -1
        if last_predict_minute:
            last_target_temp = self.get_historical(self.target_temperature, 0, default=20.0)
            for minute in range(0, self.forecast_days * 24 * 60, PREDICT_STEP):
                target_temp = self.get_historical(self.target_temperature, minute, default=20.0)
                if target_temp > last_target_temp:
                    adjust = {}
                    adjust["from"] = last_target_temp
                    adjust["to"] = target_temp
                    adjust["end"] = minute
                    adjust_ptr = 0

                    reached_minute = minute
                    for next_minute in range(minute, self.forecast_days * 24 * 60, PREDICT_STEP):
                        if last_predict_minute[next_minute] >= target_temp:
                            reached_minute = next_minute
                            break
                    adjust["reached"] = reached_minute
                    timeframe = reached_minute - minute
                    adjust["timeframe"] = timeframe
                    adjust["start"] = max(minute - timeframe, 0)
                    adjustment_points.append(adjust)
                last_target_temp = target_temp
            # self.log("Thermostat adjusts {}".format(adjustment_points))

        for minute in range(0, self.forecast_days * 24 * 60, PREDICT_STEP):
            minute_absolute = minute + self.minutes_now
            external_temp = self.temperatures.get(minute, external_temp)

            target_temp = self.get_historical(self.target_temperature, minute, default=20.0)

            # Find the next temperature adjustment
            next_adjust = None
            if adjust_ptr >= 0:
                next_adjust = adjustment_points[adjust_ptr]
            if next_adjust and minute > adjust["end"]:
                adjust_ptr += 1
                if adjust_ptr >= len(adjustment_points):
                    adjust_ptr = -1
                    next_adjust = None
                else:
                    next_adjust = adjustment_points[adjust_ptr]

            if self.smart_thermostat:
                if next_adjust and minute >= adjust["start"] and minute < adjust["end"]:
                    target_temp = adjust["to"]
                    # self.log("Predheat: Adjusted target temperature for smart heating to {} at minute {}".format(target_temp, minute))

            temp_diff_outside = internal_temp - external_temp
            temp_diff_inside = target_temp - internal_temp

            # Thermostat model, override with current state also
            if minute == 0:
                heating_on = heating_active
            elif heating_on and temp_diff_inside <= -self.hysteresis_on:
                heating_on = False
            elif not heating_on and temp_diff_inside >= self.hysteresis_off:
                heating_on = True

            heat_loss_current = self.heat_loss_watts * temp_diff_outside * PREDICT_STEP / 60.0
            heat_loss_current -= self.heat_gain_static * PREDICT_STEP / 60.0

            flow_temp = 0
            heat_to = 0
            heat_power_in = 0
            heat_power_out = 0
            if heating_on:
                out_temp = int(external_temp + 0.5)
                out_temp = min(max(out_temp, -20), 20)
                heat_to = target_temp

                if self.weather_compensation_enabled:
                    flow_temp = self.weather_compensation.get(out_temp)
                else:
                    flow_temp = self.flow_temp

                if volume_temp < flow_temp:
                    flow_temp_diff = min(flow_temp - volume_temp, self.flow_difference_target)
                    power_percent = flow_temp_diff / self.flow_difference_target
                    heat_power_out = self.heat_max_power * power_percent
                    heat_power_out = max(self.heat_min_power, heat_power_out)
                    heat_power_out = min(self.heat_max_power, heat_power_out)

                    # self.log("Minute {} flow {} volume {} diff {} power {} kw".format(minute, flow_temp, volume_temp, flow_temp_diff, heat_power_in / 1000.0))

                if self.mode == "gas":
                    # Gas boiler flow temperature adjustment in efficiency based on flow temp
                    inlet_temp = int(volume_temp + 0.5)
                    inlet_temp = min(max(inlet_temp, 0), 100)
                    condensing = self.gas_efficiency.get(inlet_temp, 0.80)
                    heat_power_in = heat_power_out / (condensing * self.heat_cop)
                else:
                    # Heat pump efficiency based on outdoor temp

                    cop_adjust = self.heat_pump_efficiency.get(out_temp, self.heat_pump_efficiency_max) / self.heat_pump_efficiency_max
                    heat_power_in = heat_power_out / (self.heat_cop * cop_adjust)

                energy_now_in = heat_power_in * PREDICT_STEP / 60.0 / 1000.0
                energy_now_out = heat_power_out * PREDICT_STEP / 60.0 / 1000.0

                cost += energy_now_in * self.rate_import.get(minute_absolute, 0)
                heat_energy += energy_now_in

                # 1.16 watts required to raise water by 1 degree in 1 hour
                volume_temp += (heat_power_out / WATTS_TO_DEGREES / self.heat_volume) * PREDICT_STEP / 60.0

            flow_delta = volume_temp - internal_temp
            flow_delta_rounded = min(max(int(flow_delta), 0), 75)
            correction = self.delta_correction.get(flow_delta_rounded, 1.0)
            heat_output = self.heat_output * correction

            # Cooling of the radiators
            volume_temp -= (heat_output / WATTS_TO_DEGREES / self.heat_volume) * PREDICT_STEP / 60.0

            heat_loss_current -= heat_output * PREDICT_STEP / 60.0

            internal_temp = internal_temp - heat_loss_current / self.watt_per_degree

            # Store for charts
            if (minute % 10) == 0:
                minute_timestamp = self.midnight_utc + timedelta(seconds=60 * minute_absolute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                internal_temp_predict_stamp[stamp] = dp2(internal_temp)
                external_temp_predict_stamp[stamp] = dp2(external_temp)
                target_temp_predict_stamp[stamp] = dp2(target_temp)
                heat_to_predict_stamp[stamp] = dp2(heat_to)
                heat_energy_predict_stamp[stamp] = dp2(heat_energy)
                volume_temp_stamp[stamp] = dp2(volume_temp)
                cost_stamp[stamp] = dp2(cost)

                entry = {}
                entry["last_updated"] = stamp
                entry["energy"] = dp2(heat_energy)
                energy_today_external.append(entry)

            # Store raw data
            target_temp_predict_minute[minute] = dp2(target_temp)
            internal_temp_predict_minute[minute] = dp2(internal_temp)
            heat_to_predict_temperature[minute] = dp2(heat_to)
            heat_energy_predict_minute[minute] = dp2(heat_energy)
            cost_minute[minute] = dp2(cost)

            if minute == 0:
                next_volume_temp = volume_temp

        if save == "best":
            self.set_state(
                self.prefix + ".internal_temp",
                state=dp2(self.internal_temperature[0]),
                attributes={"results": internal_temp_predict_stamp, "friendly_name": "Internal Temperature Predicted", "state_class": "measurement", "unit_of_measurement": "°C"},
            )
            self.set_state(
                self.prefix + ".external_temp",
                state=dp2(self.external_temperature[0]),
                attributes={"results": external_temp_predict_stamp, "friendly_name": "External Temperature Predicted", "state_class": "measurement", "unit_of_measurement": "°C"},
            )
            self.set_state(
                self.prefix + ".target_temp", state=dp2(target_temp_predict_minute[0]), attributes={"results": target_temp_predict_stamp, "friendly_name": "Target Temperature Predicted", "state_class": "measurement", "unit_of_measurement": "°C"}
            )
            self.set_state(self.prefix + ".heat_to_temp", state=dp2(heat_to_predict_temperature[0]), attributes={"results": heat_to_predict_stamp, "friendly_name": "Predict heating to target", "state_class": "measurement", "unit_of_measurement": "°C"})
            self.set_state(self.prefix + ".internal_temp_h1", state=dp2(internal_temp_predict_minute[60]), attributes={"friendly_name": "Internal Temperature Predicted +1hr", "state_class": "measurement", "unit_of_measurement": "°C"})
            self.set_state(self.prefix + ".internal_temp_h2", state=dp2(internal_temp_predict_minute[60 * 2]), attributes={"friendly_name": "Internal Temperature Predicted +2hr", "state_class": "measurement", "unit_of_measurement": "°C"})
            self.set_state(self.prefix + ".internal_temp_h8", state=dp2(internal_temp_predict_minute[60 * 8]), attributes={"friendly_name": "Internal Temperature Predicted +8hrs", "state_class": "measurement", "unit_of_measurement": "°C"})
            self.set_state(
                self.prefix + ".heat_energy",
                state=self.heat_energy_today,
                attributes={"external": energy_today_external, "results": heat_energy_predict_stamp, "friendly_name": "Predict heating energy", "state_class": "measurement", "unit_of_measurement": "kWh"},
            )
            self.set_state(self.prefix + ".heat_energy_h1", state=heat_energy_predict_minute[60], attributes={"friendly_name": "Predict heating energy +1hr", "state_class": "measurement", "unit_of_measurement": "kWh"})
            self.set_state(self.prefix + ".heat_energy_h2", state=heat_energy_predict_minute[60 * 2], attributes={"friendly_name": "Predict heating energy +2hrs", "state_class": "measurement", "unit_of_measurement": "kWh"})
            self.set_state(self.prefix + ".heat_energy_h8", state=heat_energy_predict_minute[60 * 8], attributes={"friendly_name": "Predict heating energy +8hrs", "state_class": "measurement", "unit_of_measurement": "kWh"})
            self.set_state(self.prefix + ".volume_temp", state=dp2(next_volume_temp), attributes={"results": volume_temp_stamp, "friendly_name": "Volume temperature", "state_class": "measurement", "unit_of_measurement": "°C"})
            self.set_state(self.prefix + ".cost", state=dp2(cost), attributes={"results": cost_stamp, "friendly_name": "Predicted cost", "state_class": "measurement", "unit_of_measurement": "p"})
            self.set_state(self.prefix + ".cost_h1", state=dp2(cost_minute[60]), attributes={"friendly_name": "Predicted cost +1hr", "state_class": "measurement", "unit_of_measurement": "p"})
            self.set_state(self.prefix + ".cost_h2", state=dp2(cost_minute[60 * 2]), attributes={"friendly_name": "Predicted cost +2hrs", "state_class": "measurement", "unit_of_measurement": "p"})
            self.set_state(self.prefix + ".cost_h8", state=dp2(cost_minute[60 * 8]), attributes={"friendly_name": "Predicted cost +8hrs", "state_class": "measurement", "unit_of_measurement": "p"})
        return next_volume_temp, internal_temp_predict_minute

    def today_cost(self, import_today):
        """
        Work out energy costs today (approx)
        """
        day_cost = 0
        day_cost_import = 0
        day_energy = 0
        day_cost_time = {}

        if not import_today:
            self.log("Predheat: No import data for today")
            return 0

        for minute in range(0, self.minutes_now):
            minute_back = self.minutes_now - minute - 1
            energy = 0
            energy = self.get_from_incrementing(import_today, minute_back)
            day_energy += energy

            if self.rate_import:
                day_cost += self.rate_import[minute] * energy

            if (minute % 10) == 0:
                minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                day_cost_time[stamp] = dp2(day_cost)

        self.set_state(self.prefix + ".cost_today", state=dp2(day_cost), attributes={"results": day_cost_time, "friendly_name": "Cost so far today", "state_class": "measurement", "unit_of_measurement": "p", "icon": "mdi:currency-usd"})
        self.log("Predheat: Todays energy import {} kwh cost {} p".format(dp2(day_energy), dp2(day_cost), dp2(day_cost_import)))
        return day_cost

    def update_pred(self, scheduled):
        """
        Update the heat prediction
        """
        self.had_errors = False

        local_tz = pytz.timezone(self.get_arg("timezone", "Europe/London"))
        now_utc = datetime.now(local_tz)
        now = datetime.now()
        self.forecast_days = self.get_arg("forecast_days", 2, domain="predheat")
        self.forecast_minutes = self.forecast_days * 60 * 24
        self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = int((now - self.midnight).seconds / 60 / PREDICT_STEP) * PREDICT_STEP
        self.metric_future_rate_offset_import = 0

        self.log("Predheat: update at {}".format(now_utc))
        self.days_previous = self.get_arg("days_previous", [7], domain="predheat")
        self.days_previous_weight = self.get_arg("days_previous_weight", [1 for i in range(0, len(self.days_previous))], domain="predheat")
        if len(self.days_previous) > len(self.days_previous_weight):
            # Extend weights with 1 if required
            self.days_previous_weight += [1 for i in range(0, len(self.days_previous) - len(self.days_previous_weight))]
        self.max_days_previous = max(self.days_previous) + 1
        self.heating_energy_scaling = self.get_arg("heating_energy_scaling", 1.0, domain="predheat")

        self.external_temperature, age_external = self.minute_data_entity(now_utc, "external_temperature", smoothing=True)
        self.internal_temperature, age_internal = self.minute_data_entity(now_utc, "internal_temperature", smoothing=True)
        self.target_temperature, age_target = self.minute_data_entity(now_utc, "target_temperature")
        self.heating_energy, age_energy = self.minute_data_entity(now_utc, "heating_energy", incrementing=True, smoothing=True, scaling=self.heating_energy_scaling)
        self.minute_data_age = min(age_external, age_internal, age_target, age_energy)

        self.heat_energy_today = self.heating_energy.get(0, 0) - self.heating_energy.get(self.minutes_now, 0)

        self.mode = self.get_arg("mode", "pump", domain="predheat")
        self.flow_temp = self.get_arg("flow_temp", 40.0, domain="predheat")
        if self.mode == "gas":
            self.flow_difference_target = self.get_arg("flow_difference_target", 20.0, domain="predheat")
        else:
            self.flow_difference_target = self.get_arg("flow_difference_target", 5, domain="predheat")
        self.log("Predheat: has {} days of historical data".format(self.minute_data_age))
        self.heat_loss_watts = self.get_arg("heat_loss_watts", 100, domain="predheat")
        self.heat_loss_degrees = self.get_arg("heat_loss_degrees", 0.02, domain="predheat")
        self.heat_gain_static = self.get_arg("heat_gain_static", 0, domain="predheat")
        self.watt_per_degree = self.heat_loss_watts / self.heat_loss_degrees
        self.heat_output = self.get_arg("heat_output", 7000, domain="predheat")
        self.heat_volume = self.get_arg("heat_volume", 200, domain="predheat")
        self.heat_max_power = self.get_arg("heat_max_power", 30000, domain="predheat")
        self.heat_min_power = self.get_arg("heat_min_power", 7000, domain="predheat")
        self.heat_cop = self.get_arg("heat_cop", 0.9, domain="predheat")
        self.smart_thermostat = self.get_arg("smart_thermostat", False, domain="predheat")
        self.heating_active = self.get_arg("heating_active", False, domain="predheat")
        self.next_volume_temp = self.get_arg("volume_temp", self.get_arg("next_volume_temp", self.internal_temperature[0]))

        self.log("Predheat: Hysteresis On {} off {} heat loss {} W heat loss {} W per degree".format(self.hysteresis_on, self.hysteresis_off, self.heat_loss_watts, self.heat_loss_degrees))
        self.log(
            "Predheat: Mode {} Heating active {} Heat loss watts {} degrees {} watts per degree {} heating energy so far {} volume temp {} internal temp {} external temp {}".format(
                self.mode, self.heating_active, self.heat_loss_watts, self.heat_loss_degrees, self.watt_per_degree, dp2(self.heat_energy_today), dp3(self.next_volume_temp), dp3(self.internal_temperature[0]), dp3(self.external_temperature[0])
            )
        )
        self.get_weather_data(now_utc)
        status = "idle"

        if self.mode == "gas":
            self.rate_import = self.base.rate_gas
        else:
            self.rate_import = self.base.rate_import

        # Cost so far today
        self.import_today_cost = self.today_cost(self.heating_energy)

        # Run sim
        next_volume_temp, predict_minute = self.run_simulation(self.next_volume_temp, self.heating_active)
        next_volume_temp, predict_minute = self.run_simulation(self.next_volume_temp, self.heating_active, last_predict_minute=predict_minute, save="best")
        if scheduled:
            # Update state
            self.next_volume_temp = next_volume_temp
            self.expose_config("next_volume_temp", dp3(self.next_volume_temp))

        if self.had_errors:
            self.log("Predheat: completed run status {} with Errors reported (check log)".format(status))
        else:
            self.log("Predheat: Completed run status {}".format(status))

    def initialize(self):
        """
        Setup the app, called once each time the app starts
        """
        self.log("Predheat: Startup")
        try:
            self.reset()
        except Exception as e:
            self.log("ERROR: Exception raised {}".format(e))
            self.record_status("ERROR: Exception raised {}".format(e), had_errors=True)
            raise e

        run_every = self.get_arg("run_every", 5, domain="predheat") * 60
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        seconds_now = (now - midnight).seconds

        # Calculate next run time to exactly align with the run_every time
        seconds_offset = seconds_now % run_every
        seconds_next = seconds_now + (run_every - seconds_offset)
        next_time = midnight + timedelta(seconds=seconds_next)
        self.log("Predheat: Next run time will be {} and then every {} seconds".format(next_time, run_every))

        self.update_pending = True

        # And then every N minutes
        self.run_every(self.run_time_loop, next_time, run_every, random_start=0, random_end=0)
        self.run_every(self.update_time_loop, datetime.now(), 5, random_start=0, random_end=0)

    def update_time_loop(self, cb_args):
        """
        Called every 15 seconds
        """
        if not self.get_arg("predheat_enable"):
            return

        if self.update_pending and not self.prediction_started:
            self.prediction_started = True
            self.update_pending = False
            try:
                self.update_pred(scheduled=False)
            except Exception as e:
                self.log("ERROR: Exception raised {}".format(e))
                self.record_status("ERROR: Exception raised {}".format(e), had_errors=True)
                raise e
            finally:
                self.prediction_started = False
            self.prediction_started = False

    def run_time_loop(self, cb_args):
        """
        Called every N minutes
        """
        if not self.get_arg("predheat_enable"):
            return

        if not self.prediction_started:
            self.prediction_started = True
            self.update_pending = False
            try:
                self.update_pred(scheduled=True)
            except Exception as e:
                self.log("ERROR: Exception raised {}".format(e))
                self.record_status("ERROR: Exception raised {}".format(e), had_errors=True)
                raise e
            finally:
                self.prediction_started = False
            self.prediction_started = False
