# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Learning the battery power curves from history.

- battery_charge_power_curve / battery_discharge_power_curve: the SoC taper, from full-rate charges and exports (find_charge_curve).
- battery_temperature_charge_curve: the cold-battery limit, from full-rate charges below BATTERY_TEMPERATURE_CURVE_SOC_MAX (find_battery_temperature_samples
  and the learn/merge/overlay functions below).

BatteryCurve is a mixin of Inverter, so the scanners read the inverter's own settings and history; the module-level functions are pure.
"""

from datetime import datetime, timedelta

from const import (
    BATTERY_POWER_CURVE_EXPORT_LIMIT_MARGIN,
    BATTERY_TEMPERATURE_CURVE_HALF_LIFE_DAYS,
    BATTERY_TEMPERATURE_CURVE_MAX_AGE_DAYS,
    BATTERY_TEMPERATURE_CURVE_MIN_MINUTES,
    BATTERY_TEMPERATURE_CURVE_MIN_POWER,
    BATTERY_TEMPERATURE_CURVE_SOC_MAX,
    BATTERY_TEMPERATURE_CURVE_TEMP_MAX,
    EXPORT_LIMIT_DEFAULT_W,
    MINUTE_WATT,
)
from utils import calc_percent_limit, dp0, dp1, dp2, dp4, find_battery_temperature_cap, is_entity_id, minute_data, minute_data_state

# Largest SoC jump (percentage points) between consecutive readings that the charge curve learner
# accepts as genuine. Wide enough for inverters that report SoC in 5% steps; a larger jump is a
# glitch (e.g. a stray 0% reading) and would otherwise stamp one sample across a huge SoC range.
CHARGE_CURVE_MAX_SOC_STEP = 10


def _temperature_sample_minutes(day_bins):
    """
    Total charging minutes held in one day of battery temperature samples ({temperature: [sum_of_c, minutes]}).
    """
    return sum(entry[1] for entry in day_bins.values())


def clean_battery_temperature_samples(data):
    """
    Turn battery temperature samples read back from storage into {day: {int temperature: [float sum_of_c, int minutes]}}.

    Storage round-trips through JSON, so the temperature keys come back as strings; anything malformed is dropped
    rather than trusted, so a corrupt cache can only lose what was learned, never poison the curve.
    """
    cleaned = {}
    if not isinstance(data, dict):
        return cleaned
    for day, day_bins in data.items():
        try:
            datetime.strptime(str(day), "%Y-%m-%d")
        except ValueError:
            continue
        if not isinstance(day_bins, dict):
            continue
        clean_bins = {}
        for temp, entry in day_bins.items():
            try:
                temp = int(temp)
                rate_sum, minutes = float(entry[0]), int(entry[1])
            except (ValueError, TypeError, IndexError, KeyError):
                continue
            if minutes > 0 and rate_sum >= 0 and temp != 0 and temp < BATTERY_TEMPERATURE_CURVE_TEMP_MAX:
                clean_bins[temp] = [rate_sum, minutes]
        if clean_bins:
            cleaned[str(day)] = clean_bins
    return cleaned


def merge_battery_temperature_samples(stored, scanned, today, max_age_days=BATTERY_TEMPERATURE_CURVE_MAX_AGE_DAYS):
    """
    Merge the samples from a fresh history scan into the stored samples, and drop days older than max_age_days.

    Each scan re-reads the whole history window, so a day is replaced rather than added to - adding would count
    the same minutes again every scan. The oldest day of a window is only partly covered (the window starts part way
    through it), so a scanned day only replaces a stored one when it holds at least as many charging minutes.

    Args:
        stored: {day: {temperature: [sum_of_c, minutes]}} as kept in storage
        scanned: the same shape, from Inverter.find_battery_temperature_samples()
        today: datetime.date the ages are measured from
        max_age_days: days kept

    Returns:
        A new merged dictionary; stored and scanned are not modified.
    """
    merged = {day: {temp: list(entry) for temp, entry in day_bins.items()} for day, day_bins in stored.items()}
    for day, day_bins in scanned.items():
        if day not in merged or _temperature_sample_minutes(day_bins) >= _temperature_sample_minutes(merged[day]):
            merged[day] = {temp: list(entry) for temp, entry in day_bins.items()}
    for day in list(merged.keys()):
        age = (today - datetime.strptime(day, "%Y-%m-%d").date()).days
        if age > max_age_days:
            del merged[day]
    return merged


def learn_battery_temperature_curve(samples, today, min_minutes=BATTERY_TEMPERATURE_CURVE_MIN_MINUTES, half_life_days=BATTERY_TEMPERATURE_CURVE_HALF_LIFE_DAYS):
    """
    Work out a battery_temperature_charge_curve from stored samples.

    Each whole degree's rate is the mean of its charging minutes, with every day weighted down by its age
    (halving every half_life_days) so recent behaviour wins. A degree is only learned once it has min_minutes of
    charging behind it, so one short charge cannot set the rate for a whole winter.

    Args:
        samples: {day: {temperature: [sum_of_c, minutes]}}
        today: datetime.date the ages are measured from

    Returns:
        ({temperature: rate in C}, {temperature: minutes}) for the learned degrees only
    """
    weighted_sum = {}
    weighted_minutes = {}
    minutes = {}
    for day, day_bins in samples.items():
        age = max((today - datetime.strptime(day, "%Y-%m-%d").date()).days, 0)
        weight = 0.5 ** (age / half_life_days) if half_life_days > 0 else 1.0
        for temp, (rate_sum, count) in day_bins.items():
            weighted_sum[temp] = weighted_sum.get(temp, 0.0) + rate_sum * weight
            weighted_minutes[temp] = weighted_minutes.get(temp, 0.0) + count * weight
            minutes[temp] = minutes.get(temp, 0) + count
    curve = {}
    curve_minutes = {}
    for temp in sorted(minutes):
        if minutes[temp] >= min_minutes and weighted_minutes[temp] > 0:
            curve[temp] = round(weighted_sum[temp] / weighted_minutes[temp], 2)
            curve_minutes[temp] = minutes[temp]
    return curve, curve_minutes


def overlay_battery_temperature_curve(configured, learned):
    """
    Lay a learned battery temperature charge curve over the configured one.

    The learned curve replaces the configured one over the whole range it covers, from the coldest to the warmest
    learned degree: a degree with no data of its own inside that range is interpolated in a straight line from the
    learned degrees either side, as this battery's own neighbours are better evidence than a curve written for
    batteries in general. Outside that range the configured curve still applies, and below the coldest learned degree
    (down to 1) a degree the configured curve does not set takes the coldest learned rate, so there is no hole that
    falls back to the 20 degree value and plans an uncapped charge in the cold - a colder battery is never expected
    to charge faster. 0 is never filled, because an exact 0 is how several integrations report a missing reading.

    Args:
        configured: battery_temperature_charge_curve from apps.yaml, {int: float}
        learned: {int: float} from learn_battery_temperature_curve()

    Returns:
        The combined curve, a new dictionary.
    """
    curve = dict(configured)
    if not learned:
        return curve
    curve.update(learned)
    temps = sorted(learned)
    coldest = temps[0]
    for temp in range(1, coldest):
        if temp not in configured:
            curve[temp] = learned[coldest]
    for lower, upper in zip(temps, temps[1:]):
        for temp in range(lower + 1, upper):
            if temp != 0:
                fraction = (temp - lower) / (upper - lower)
                curve[temp] = round(learned[lower] + (learned[upper] - learned[lower]) * fraction, 2)
    return curve


class BatteryCurve:
    """Mixin for Inverter: scans the inverter's history for the battery power curves."""

    def find_charge_curve(self, discharge):
        """
        Find expected charge curve
        """
        curve_type = "charge"
        if discharge:
            curve_type = "discharge"

        predbat_status_sensor = self.base.prefix + ".status"

        if "soc_percent" in self.base.args:
            soc_kwh_sensor = self.base.get_arg("soc_percent", indirect=False, index=self.id)
            soc_kwh_percent = True
            soc_label = "soc_percent"
        else:
            soc_kwh_percent = False
            soc_label = "soc_kw"
            soc_kwh_sensor = self.base.get_arg("soc_kw", indirect=False, index=self.id)

        if discharge:
            charge_rate_sensor = self.base.get_arg("discharge_rate", indirect=False, index=self.id)
            curve_label = "empty"
        else:
            charge_rate_sensor = self.base.get_arg("charge_rate", indirect=False, index=self.id)
            curve_label = "full"

        battery_power_sensor = self.base.get_arg("battery_power", indirect=False, index=self.id)
        battery_power_invert = self.base.get_arg("battery_power_invert", False, index=self.id)

        final_curve = {}
        final_curve_count = {}

        if discharge:
            max_power = int(self.battery_rate_max_discharge * MINUTE_WATT)
        else:
            max_power = int(self.battery_rate_max_charge * MINUTE_WATT)

        max_power_scaled = max_power * 0.95

        if soc_kwh_sensor and charge_rate_sensor and battery_power_sensor and predbat_status_sensor:
            battery_power_sensor = battery_power_sensor.replace("number.", "sensor.")  # Workaround as old template had number.
            self.log("Inverter {} Looking for {} curve with sensors {}, {}, {} and {}".format(self.id, curve_type, soc_kwh_sensor, charge_rate_sensor, predbat_status_sensor, battery_power_sensor))
            if soc_kwh_percent:
                soc_kwh_data = self.base.get_history_wrapper(entity_id=soc_kwh_sensor, days=self.base.max_days_previous, required=False)
            else:
                soc_kwh_data = self.base.get_history_wrapper(entity_id=soc_kwh_sensor, days=self.base.max_days_previous, required=False)
            charge_rate_data = self.base.get_history_wrapper(entity_id=charge_rate_sensor, days=self.base.max_days_previous, required=False)
            battery_power_data = self.base.get_history_wrapper(entity_id=battery_power_sensor, days=self.base.max_days_previous, required=False)
            predbat_status_data = self.base.get_history_wrapper(entity_id=predbat_status_sensor, days=self.base.max_days_previous, required=False)

            if soc_kwh_data and charge_rate_data and battery_power_data and predbat_status_data:
                if soc_kwh_percent:
                    # If its in percent convert to kWh
                    soc_kwh, ignore_io = minute_data(
                        soc_kwh_data[0],
                        self.base.max_days_previous,
                        self.base.now_utc,
                        "state",
                        "last_updated",
                        backwards=True,
                        clean_increment=False,
                        smoothing=False,
                        divide_by=1.0,
                        scale=1.0,  # A percent is already of usable capacity, and soc_max already includes battery_scaling (#6183)
                        required_unit="%",
                        can_modify_history=True,  # history is not accessed after this point, so minute_data can freely modify it
                    )
                    for entry in soc_kwh:
                        soc_kwh[entry] = dp4(soc_kwh[entry] * self.soc_max / 100.0)
                else:
                    soc_kwh, ignore_io = minute_data(
                        soc_kwh_data[0],
                        self.base.max_days_previous,
                        self.base.now_utc,
                        "state",
                        "last_updated",
                        backwards=True,
                        clean_increment=False,
                        smoothing=False,
                        divide_by=1.0,
                        scale=self.battery_scaling,
                        required_unit="kWh",
                        can_modify_history=True,  # history is not accessed after this point, so minute_data can freely modify it
                    )
                charge_rate, ignore_io = minute_data(
                    charge_rate_data[0],
                    self.base.max_days_previous,
                    self.base.now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    clean_increment=False,
                    smoothing=False,
                    divide_by=1.0,
                    scale=1.0,
                    required_unit="W",
                    can_modify_history=True,  # history is not accessed after this point, so minute_data can freely modify it
                )
                predbat_status = minute_data_state(predbat_status_data[0], self.base.max_days_previous, self.base.now_utc, "state", "last_updated")
                for minute in predbat_status:
                    status = predbat_status[minute]
                    if "," in status:
                        # If there are multiple statuses take the first one
                        predbat_status[minute] = status.split(",")[0].strip()
                battery_power, ignore_io = minute_data(
                    battery_power_data[0],
                    self.base.max_days_previous,
                    self.base.now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    clean_increment=False,
                    smoothing=False,
                    divide_by=1.0,
                    scale=1.0,
                    required_unit="W",
                    can_modify_history=True,  # history is not accessed after this point, so minute_data can freely modify it
                )
                if battery_power_invert:
                    # Invert the battery power if required
                    for minute in battery_power:
                        battery_power[minute] = -battery_power[minute]
                min_len = min(len(soc_kwh), len(charge_rate), len(predbat_status), len(battery_power))
                self.log("Inverter {} Looking for {} curve, have found {} days of history data, max days {}".format(self.id, curve_type, dp1(min_len / 60 / 24.0), self.base.max_days_previous))

                soc_percent = {}
                for minute in range(0, min_len):
                    soc_percent[minute] = calc_percent_limit(soc_kwh.get(minute, 0), self.soc_max)

                temperature_limited = self.find_temperature_limited_minutes(discharge, min_len, max_power_scaled)
                if discharge:
                    export_limited = self.find_export_limited_minutes(min_len, max_power_scaled)
                    if export_limited is None:
                        return {}
                    temperature_limited = temperature_limited | export_limited

                if discharge:
                    search_range = range(5, 20, 1)
                else:
                    search_range = range(99, 85, -1)

                # Find 100% end points. The exact-match checks below ("Charging" / "Exporting",
                # "Discharging") deliberately exclude "Cross-charging" minutes - during genuine
                # cross-charging another inverter is simultaneously drawing/feeding power at the
                # same time, so this inverter's battery_power reading isn't a clean single-inverter
                # charge/discharge sample and would corrupt the learned curve if included.
                for data_point in search_range:
                    for minute in range(1, min_len):
                        # Start trigger is when the SoC just increased above the data point
                        previous_point = soc_percent.get(minute - 1, 0)
                        if (
                            not discharge
                            and previous_point > data_point
                            and soc_percent.get(minute, 0) == data_point
                            and predbat_status.get(minute - 1, "") == "Charging"
                            and predbat_status.get(minute, "") == "Charging"
                            and predbat_status.get(minute + 1, "") == "Charging"
                            and charge_rate.get(minute - 1, 0) >= max_power_scaled
                            and charge_rate.get(minute, 0) >= max_power_scaled
                            and battery_power.get(minute, 0) < 0
                        ) or (
                            discharge
                            and previous_point < data_point
                            and soc_percent.get(minute, 0) == data_point
                            and predbat_status.get(minute - 1, "") in ["Exporting", "Discharging"]
                            and predbat_status.get(minute, "") in ["Exporting", "Discharging"]
                            and predbat_status.get(minute + 1, "") in ["Exporting", "Discharging"]
                            and charge_rate.get(minute - 1, 0) >= max_power_scaled
                            and charge_rate.get(minute, 0) >= max_power_scaled
                            and battery_power.get(minute, 0) > 0
                        ):
                            total_power = 0
                            total_count = 0
                            # Find a period where charging was at full rate and the SoC just drops below the data point
                            for target_minute in range(minute, min_len):
                                this_soc = soc_percent.get(target_minute, 0)
                                if not discharge and (predbat_status.get(target_minute, "") != "Charging" or charge_rate.get(target_minute, 0) < max_power_scaled or battery_power.get(target_minute, 0) >= 0):
                                    break
                                if discharge and (predbat_status.get(target_minute, "") not in ["Exporting", "Discharging"] or charge_rate.get(target_minute, 0) < max_power_scaled or battery_power.get(target_minute, 0) <= 0):
                                    break
                                if target_minute in temperature_limited:
                                    # A cold battery (or, discharging, the export limit) was holding the rate down, which is not the SoC curve
                                    break

                                if (discharge and (this_soc > data_point)) or (not discharge and (this_soc < data_point)):
                                    if total_power == 0:
                                        # No power data, so skip this data point
                                        break
                                    if target_minute not in soc_kwh or abs(previous_point - data_point) > CHARGE_CURVE_MAX_SOC_STEP or abs(data_point - this_soc) > CHARGE_CURVE_MAX_SOC_STEP:
                                        # Missing or implausible SoC reading at either end of the step, skip this sample (#5259)
                                        break
                                    if discharge:
                                        this_soc -= 1
                                    else:
                                        this_soc += 1
                                    # So the power for this data point average has been stored, it's possible we spanned more than one data point
                                    # if not all SoC %'s are represented for this battery size
                                    from_soc = soc_kwh.get(minute, 0)
                                    to_soc = soc_kwh.get(target_minute, 0)
                                    soc_charged = from_soc - to_soc
                                    average_power = total_power / total_count
                                    charge_curve = round(min(average_power / max_power / self.base.battery_loss, 1.0), 2)
                                    if self.base.debug_enable:
                                        self.log(
                                            "Curve Percent: {}-{} at {} took {} minutes charged {} curve {} average_power {}".format(
                                                data_point,
                                                this_soc,
                                                self.base.time_abs_str(self.base.minutes_now - minute),
                                                total_count,
                                                round(soc_charged, 2),
                                                charge_curve,
                                                average_power,
                                            )
                                        )
                                    # Store data points
                                    if discharge:
                                        store_range = range(this_soc, previous_point, -1)
                                    else:
                                        store_range = range(this_soc, previous_point)

                                    for point in store_range:
                                        if point not in final_curve:
                                            final_curve[point] = charge_curve
                                            final_curve_count[point] = 1
                                        else:
                                            final_curve[point] += charge_curve
                                            final_curve_count[point] += 1

                                    break
                                else:
                                    # Store data for this minute of the period, so total_power/total_count
                                    # below is the mean power across the whole SoC step rather than the
                                    # reading at the single minute the step was triggered on
                                    total_power += abs(battery_power.get(target_minute, 0))
                                    total_count += 1
                if final_curve:
                    # Average the data points
                    for index in final_curve:
                        if final_curve_count[index] > 0:
                            final_curve[index] = dp2(final_curve[index] / final_curve_count[index])

                    self.log("Inverter {} {} curve before adjustment is: {}".format(self.id, curve_type, final_curve))

                    # Find info for gap filling
                    found_required = False
                    if discharge:
                        fill_range = range(4, 21, 1)
                        value = min(final_curve.values())
                    else:
                        value = max(final_curve.values())
                        fill_range = range(85, 101)

                    # Pick the first value point for fill below
                    for point in fill_range:
                        if point in final_curve:
                            value = final_curve[point]
                            break

                    # Fill gaps
                    for point in fill_range:
                        if point not in final_curve:
                            final_curve[point] = value
                            found_required = True
                        else:
                            value = final_curve[point]

                    if found_required:
                        # Scale curve to 1.0
                        rate_scaling = 0

                        # Work out the maximum power to use as a scale factor
                        for index in final_curve:
                            rate_scaling = max(final_curve[index], rate_scaling)

                        # Scale the curve
                        if rate_scaling > 0:
                            for index in final_curve:
                                final_curve[index] = round(final_curve[index] / rate_scaling, 2)

                        # If we have the correct data then output it
                        if rate_scaling > 0:
                            if discharge:
                                text = "  battery_discharge_power_curve:\n"
                            else:
                                text = "  battery_charge_power_curve:\n"
                            keys = sorted(final_curve.keys())
                            keys.reverse()
                            first = True
                            for key in keys:
                                if (final_curve[key] < 1.0 or first) and final_curve[key] > 0.0:
                                    text += "    {} : {}\n".format(key, final_curve[key])
                                    first = False
                            if self.base.battery_discharge_power_curve_auto if discharge else self.base.battery_charge_power_curve_auto:
                                self.log("Info: Inverter {} curve automatically computed as:\n".format(self.id) + text)
                            else:
                                self.log("Info: Inverter {} {} curve can be entered into apps.yaml or set to auto:\n".format(self.id, curve_type) + text)
                            rate_scaling = round(rate_scaling, 2)
                            if discharge:
                                if rate_scaling != self.base.battery_rate_max_scaling_discharge:
                                    self.log("Info: Inverter {} Consider setting in HA: input_number.battery_rate_max_scaling_discharge: {} - currently {}".format(self.id, rate_scaling, self.base.battery_rate_max_scaling_discharge))
                            else:
                                if rate_scaling != self.base.battery_rate_max_scaling:
                                    self.log("Info: Inverter {} Consider setting in HA: input_number.battery_rate_max_scaling: {} - currently {}".format(self.id, rate_scaling, self.base.battery_rate_max_scaling))
                            return final_curve
                        else:
                            self.log("Info: Inverter {} Found incorrect battery {} curve (was 0), maybe try again when you have more data.".format(self.id, curve_type))
                    else:
                        self.log("Warn: Inverter {} Found incomplete battery {} curve (no data points), maybe try again when you have more data.".format(self.id, curve_type))
                else:
                    self.log(
                        "Info: Cannot find battery {} curve (no full rate {} cycle to {} found in history), battery may not have been fully charged/discharged at max rate recently - this is normal in cost-optimised operation".format(
                            curve_type, curve_type, curve_label
                        )
                    )
            else:
                history_checks = [
                    (soc_label, soc_kwh_sensor, soc_kwh_data),
                    ("{}_rate".format(curve_type), charge_rate_sensor, charge_rate_data),
                    ("battery_power", battery_power_sensor, battery_power_data),
                    ("predbat.status", predbat_status_sensor, predbat_status_data),
                ]
                no_history = ["{} ({})".format(setting, entity_id) for setting, entity_id, data in history_checks if not data]
                self.log("Warn: Inverter {} Cannot find battery {} curve (missing history), no history found for {} - check the entity name in apps.yaml and that Home Assistant is recording its history".format(self.id, curve_type, ", ".join(no_history)))
                self.log("Warn: Inverter {} Sensor history record counts: {}".format(self.id, ", ".join("{} {}".format(setting, len(data[0]) if data else 0) for setting, entity_id, data in history_checks)))
        else:
            missing_settings = [setting for setting, entity_id in [(soc_label, soc_kwh_sensor), ("{}_rate".format(curve_type), charge_rate_sensor), ("battery_power", battery_power_sensor)] if not entity_id]
            self.log("Warn: Inverter {} Cannot find battery {} curve (settings missing), {} not set in apps.yaml".format(self.id, curve_type, ", ".join(missing_settings)))
        return {}

    def find_temperature_limited_minutes(self, discharge, min_len, max_power_scaled):
        """
        Find the minutes of history where the battery temperature curve says the battery could not run at its full rate.

        find_charge_curve() learns the high-SoC taper from full-rate charges (or the low-SoC tail-off from full-rate
        discharges), and a cold battery held back by its BMS looks exactly like a taper, so those minutes are left out.
        A minute is limited when find_battery_temperature_cap() at its temperature, with the configured (or learned)
        battery_temperature_charge_curve / battery_temperature_discharge_curve, is below max_power_scaled. Readings of
        exactly 0 are treated as missing, as several integrations report a missing temperature as 0.

        Args:
            discharge: True to use the discharge temperature curve
            min_len: minutes of history being searched
            max_power_scaled: the rate in W a minute must reach to count as full rate

        Returns:
            A set of minute indexes (minutes before now); empty when there is no temperature curve or history.
        """
        temperature_curve = self.base.battery_temperature_discharge_curve if discharge else self.base.battery_temperature_charge_curve
        temperature_sensor = self.base.get_arg("battery_temperature_history", indirect=False)
        if not temperature_curve or not temperature_sensor or self.soc_max <= 0:
            return set()
        temperature_data = self.base.get_history_wrapper(entity_id=temperature_sensor, days=self.base.max_days_previous, required=False)
        if not temperature_data:
            return set()
        temperature, _ = minute_data(
            temperature_data[0],
            self.base.max_days_previous,
            self.base.now_utc,
            "state",
            "last_updated",
            backwards=True,
            clean_increment=False,
            smoothing=False,
            divide_by=1.0,
            scale=1.0,
            can_modify_history=True,  # history is not accessed after this point
        )
        limited = set()
        cap_cache = {}
        for minute in range(0, min_len):
            temp = temperature.get(minute, None)
            if temp is None or temp == 0:
                continue
            temp_idx = int(min(max(temp, -20), 20))
            if temp_idx not in cap_cache:
                cap_cache[temp_idx] = find_battery_temperature_cap(temp_idx, temperature_curve, self.soc_max, float("inf")) * MINUTE_WATT
            if cap_cache[temp_idx] < max_power_scaled:
                limited.add(minute)
        if limited:
            self.log("Inverter {} Leaving {} temperature-limited minutes out of the battery {} curve".format(self.id, len(limited), "discharge" if discharge else "charge"))
        return limited

    def find_export_limited_minutes(self, min_len, max_power_scaled):
        """
        Find the minutes of history where the export limit, not the battery, held back a discharge.

        While exporting, the battery supplies the house load plus the grid export, so once the export reaches
        export_limit the inverter turns the battery down and the discharge rate drops for a reason that has nothing to
        do with the SoC. A minute counts as limited when grid_power (positive for export) was at or above
        BATTERY_POWER_CURVE_EXPORT_LIMIT_MARGIN of export_limit.

        Args:
            min_len: minutes of history being searched
            max_power_scaled: the rate in W a minute must reach to count as full rate

        Returns:
            A set of minute indexes (minutes before now): empty when no export limit is set, or when there is no
            grid_power history and the limit is above the full rate (so it cannot be what held the battery back).
            None when the limit is below the full rate and there is no grid_power history to tell the minutes apart,
            as the discharge curve cannot then be learned safely.
        """
        export_limit_w = self.export_limit * MINUTE_WATT
        if export_limit_w >= EXPORT_LIMIT_DEFAULT_W:
            return set()
        grid_sensor = self.base.get_arg("grid_power", indirect=False, index=self.id)
        grid_data = None
        if grid_sensor and is_entity_id(grid_sensor):
            grid_data = self.base.get_history_wrapper(entity_id=grid_sensor, days=self.base.max_days_previous, required=False)
        if not grid_data:
            if export_limit_w < max_power_scaled:
                self.log("Info: Inverter {} Cannot find battery discharge curve, export_limit {}W is below the discharge rate and there is no grid_power history to tell which exports it limited".format(self.id, dp0(export_limit_w)))
                return None
            return set()
        grid_power, _ = minute_data(
            grid_data[0],
            self.base.max_days_previous,
            self.base.now_utc,
            "state",
            "last_updated",
            backwards=True,
            clean_increment=False,
            smoothing=False,
            divide_by=1.0,
            scale=1.0,
            required_unit="W",
            can_modify_history=True,  # history is not accessed after this point
        )
        invert = self.base.get_arg("grid_power_invert", False, index=self.id)
        threshold = export_limit_w * BATTERY_POWER_CURVE_EXPORT_LIMIT_MARGIN
        limited = set()
        for minute in range(0, min_len):
            power = grid_power.get(minute, None)
            if power is None:
                continue
            if invert:
                power = -power
            if power >= threshold:
                limited.add(minute)
        if limited:
            self.log("Inverter {} Leaving {} export-limited minutes out of the battery discharge curve".format(self.id, len(limited)))
        return limited

    def find_battery_temperature_samples(self):
        """
        Scan the recent history for minutes that show how fast the battery accepts charge at each temperature.

        A minute counts when Predbat was Charging, the charge rate was written at (nearly) its maximum, the battery
        was taking charge and the SoC was below BATTERY_TEMPERATURE_CURVE_SOC_MAX, so the high-SoC taper (which
        battery_charge_power_curve models) cannot be mistaken for a cold battery. The rate the battery actually
        took is recorded in C (kW per kWh of soc_max), the unit battery_temperature_charge_curve uses.

        Returns:
            {day: {temperature: [sum_of_c, minutes]}} keyed by the UTC date (YYYY-MM-DD) and the whole degree
            the temperature truncates to (as find_battery_temperature_cap() looks it up), or None when the
            sensors or their history are missing.
        """
        predbat_status_sensor = self.base.prefix + ".status"
        soc_kwh_percent = "soc_percent" in self.base.args
        if soc_kwh_percent:
            soc_sensor = self.base.get_arg("soc_percent", indirect=False, index=self.id)
        else:
            soc_sensor = self.base.get_arg("soc_kw", indirect=False, index=self.id)
        charge_rate_sensor = self.base.get_arg("charge_rate", indirect=False, index=self.id)
        battery_power_sensor = self.base.get_arg("battery_power", indirect=False, index=self.id)
        battery_power_invert = self.base.get_arg("battery_power_invert", False, index=self.id)
        temperature_sensor = self.base.get_arg("battery_temperature_history", indirect=False)

        if not (soc_sensor and charge_rate_sensor and battery_power_sensor and temperature_sensor) or self.soc_max <= 0:
            self.log("Warn: Inverter {} Cannot learn the battery temperature charge curve, soc, charge_rate, battery_power and battery_temperature_history must all be set in apps.yaml".format(self.id))
            return None
        battery_power_sensor = battery_power_sensor.replace("number.", "sensor.")  # Workaround as old template had number.

        days = self.base.max_days_previous
        history = {}
        for name, entity_id in [("soc", soc_sensor), ("charge_rate", charge_rate_sensor), ("battery_power", battery_power_sensor), ("temperature", temperature_sensor), ("status", predbat_status_sensor)]:
            data = self.base.get_history_wrapper(entity_id=entity_id, days=days, required=False)
            if not data:
                self.log("Warn: Inverter {} Cannot learn the battery temperature charge curve, no history found for {} ({})".format(self.id, name, entity_id))
                return None
            history[name] = data[0]

        def to_minutes(entries, scale=1.0, required_unit=None):
            values, _ = minute_data(
                entries,
                days,
                self.base.now_utc,
                "state",
                "last_updated",
                backwards=True,
                clean_increment=False,
                smoothing=False,
                divide_by=1.0,
                scale=scale,
                required_unit=required_unit,
                can_modify_history=True,  # history is not accessed after this point
            )
            return values

        # A percent is already of usable capacity and soc_max already includes battery_scaling, only kWh is scaled (#6183)
        soc = to_minutes(history["soc"], scale=1.0 if soc_kwh_percent else self.battery_scaling, required_unit="%" if soc_kwh_percent else "kWh")
        charge_rate = to_minutes(history["charge_rate"], required_unit="W")
        battery_power = to_minutes(history["battery_power"], required_unit="W")
        temperature = to_minutes(history["temperature"])
        status = minute_data_state(history["status"], days, self.base.now_utc, "state", "last_updated")

        max_power = self.battery_rate_max_charge * MINUTE_WATT
        min_charge_rate = max_power * 0.95
        min_battery_power = max(max_power * BATTERY_TEMPERATURE_CURVE_MIN_POWER, 1.0)
        battery_loss = self.base.battery_loss if self.base.battery_loss > 0 else 1.0
        samples = {}
        for minute in range(0, min(len(soc), len(charge_rate), len(battery_power), len(temperature), len(status))):
            this_status = status.get(minute, "")
            if this_status.split(",")[0].strip() != "Charging":
                continue
            if charge_rate.get(minute, 0) < min_charge_rate:
                continue
            power = battery_power.get(minute, 0)
            if battery_power_invert:
                power = -power
            if power >= 0 or abs(power) < min_battery_power:
                # Not charging, or so little that the charge has finished rather than been limited
                continue
            soc_percent = soc.get(minute, None)
            if soc_percent is None:
                continue
            if not soc_kwh_percent:
                soc_percent = soc_percent / self.soc_max * 100.0
            if soc_percent >= BATTERY_TEMPERATURE_CURVE_SOC_MAX:
                continue
            temp = temperature.get(minute, None)
            if temp is None:
                continue
            temp = int(max(temp, -20))
            if temp == 0 or temp >= BATTERY_TEMPERATURE_CURVE_TEMP_MAX:
                # The 0 entry is the fallback for every reading at or below 0, including the exact 0 several integrations
                # report for a missing reading, so it is never learned (anything between -1 and 1 truncates to it)
                continue
            rate_c = abs(power) / 1000.0 / battery_loss / self.soc_max
            day = (self.base.now_utc - timedelta(minutes=minute)).strftime("%Y-%m-%d")
            day_bins = samples.setdefault(day, {})
            entry = day_bins.setdefault(temp, [0.0, 0])
            entry[0] += rate_c
            entry[1] += 1

        for day_bins in samples.values():
            for entry in day_bins.values():
                entry[0] = dp4(entry[0])
        self.log("Inverter {} Battery temperature curve: found {} charging minutes below {}% SoC in {} days of history".format(self.id, sum(entry[1] for day_bins in samples.values() for entry in day_bins.values()), BATTERY_TEMPERATURE_CURVE_SOC_MAX, days))
        return samples
