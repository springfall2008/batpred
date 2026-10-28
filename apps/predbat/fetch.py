# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# pyright: reportAttributeAccessIssue=false


"""Data fetching module for energy rates, consumption, and forecasts.

Orchestrates loading of import/export energy rates from multiple sources
(Octopus, Energi Data Service, manual config), historical load and PV data,
battery temperature, and other sensor inputs. Converts all data to per-minute
dictionaries for use by the prediction engine.
"""

from datetime import datetime, timedelta
from utils import minutes_to_time, str2time, dp1, dp2, dp3, dp4, time_string_to_stamp, minute_data, get_now_from_cumulative, MinuteArray, strptime_aware, is_entity_id
from const import (
    MINUTE_WATT,
    PREDICT_STEP,
    TIME_FORMAT,
    PREDBAT_MODE_OPTIONS,
    PREDBAT_MODE_CONTROL_SOC,
    PREDBAT_MODE_CONTROL_CHARGEDISCHARGE,
    PREDBAT_MODE_CONTROL_CHARGE,
    PREDBAT_MODE_MONITOR,
    LOAD_FORECAST_HISTORY_MAX_DAYS,
    LOAD_FORECAST_RECORDED_MIN_MINUTES,
    PREDBAT_MAX_CARS,
    CAR_CHARGING_LIMIT_UNCAPPED,
    CAR_CHARGING_NOW_POWER_W,
    CAR_MANUAL_SOC_LATE_MINUTES,
    CAR_MANUAL_SOC_UNPLUG_MINUTES,
    CAR_MANUAL_SOC_IDLE_MINUTES,
    CAR_MANUAL_SOC_RATE_MARGIN,
    CAR_MANUAL_SOC_ENERGY_SLACK,
    CLOUD_WINDOW_MINUTES,
    CLOUD_ARRAY_MARGIN,
    PV_ARRAY_KWP_UNKNOWN,
)
from predbat_metrics import metrics
from futurerate import FutureRate
from axle import fetch_axle_sessions, load_axle_slot, fetch_axle_active

import ast
import copy

# Car state that changes at run time and that the plan reads (the rest is configuration, which a debug yaml holds)
REPLAY_CAR_FIELDS = (
    "car_charging_planned",
    "car_charging_now",
    "car_charging_soc",
    "car_charging_soc_next",
    "car_charging_limit",
    "car_charging_limit_model",
    "car_charging_battery_size",
    "car_charging_slots",
    "car_energy_reported_load",
)
# The inverter's programmed state as fetched each cycle, which the plan and prediction start from
REPLAY_INVERTER_FIELDS = (
    "charge_window",
    "charge_limit",
    "export_window",
    "export_limits",
    "isCharging",
    "isCharging_Target",
    "isExporting",
    "isExporting_Target",
    "reserve",
    "reserve_current",
    "battery_rate_max_charge",
    "battery_rate_max_discharge",
)


class Fetch:
    """Data fetching mixin for loading energy rates, consumption, and forecasts.

    Orchestrates loading of import/export rates from multiple sources,
    historical load/PV data, battery temperature, car charging energy,
    and other sensor inputs. Converts all data to per-minute dictionaries.
    """

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
            self.log("PV Forecast {}kWh and 10% Forecast {}kWh; PV cloud factor {}".format(dp1(pv_total), dp1(pv_total10), pv_factor_round))
            return pv_factor
        else:
            return None

    def get_cloud_duty(self, minutes_now, pv_data, pv_data10, pv_data90):
        """Work out how many steps of each conservation window the envelope model spends raised.

        Peaks reach the p90 ceiling and troughs reach the p10 floor together only when the up:down
        step ratio matches the band's own asymmetry, (p50-p10):(p90-p50). Solcast's downside is
        typically about twice its upside, so a fixed 1:1 alternation - all the pairwise model could
        express - always under-reaches on one side.

        Returns (n_up, n_down), or None when there is no upside band to reach for: a forecast source
        that publishes no p90 falls back to a copy of the p50, and the caller must keep the legacy
        proportional model rather than silently losing the cloud model altogether.
        """
        up_gap = 0.0
        down_gap = 0.0
        for minute in range(self.forecast_minutes):
            nominal = pv_data.get(minute + minutes_now, 0.0)
            up_gap += max(pv_data90.get(minute + minutes_now, 0.0) - nominal, 0.0)
            down_gap += max(nominal - pv_data10.get(minute + minutes_now, 0.0), 0.0)

        if up_gap <= 0:
            return None

        period = CLOUD_WINDOW_MINUTES // PREDICT_STEP
        ratio = down_gap / up_gap
        # Clamped to leave at least one step on each side - a window that is all up or all down has
        # nothing to trade against and would leave the borrow outstanding.
        n_up = int(min(max(round(period * ratio / (1.0 + ratio)), 1), period - 1))
        return (n_up, period - n_up)

    def resolve_pv_array_kwp(self, detected_kwp):
        """Settle the DC array size the p90 cloud ceiling is capped against.

        Forecast Solar and Open-Meteo declare kwp per array and fetch_pv_forecast already totals it,
        so most setups need no configuration. Solcast and the HA integrations publish no array size
        and arrive as the PV_ARRAY_KWP_UNKNOWN sentinel, leaving the cap inert - correct, since an
        invented figure would be worse than none. apps.yaml wins over both: declared figures are
        routinely understated, users entering the inverter rating or one string of two.
        """
        configured = self.get_arg("pv_array_kwp", 0.0)
        if configured and configured > 0:
            self.pv_array_kwp = float(configured)
        elif detected_kwp and 0 < detected_kwp < PV_ARRAY_KWP_UNKNOWN:
            self.pv_array_kwp = float(detected_kwp)
        else:
            self.pv_array_kwp = 0.0
        return self.pv_array_kwp

    def get_pv90_cloud_ceiling(self, minutes_now, pv_data, pv_data90):
        """Build the per-minute ceiling the p90 series modulates toward.

        p50 reaches for p90 and p10 reaches for p50, but p90 has no next percentile above it, so its
        upside is extrapolated by continuing the band one width further. Leaving p90 unmodulated
        instead is not neutral: it makes p90 the one smooth scenario, handing it an efficiency
        bonus the other two do not get and biasing every comparison between them.

        The extrapolation is capped at the DC array size times CLOUD_ARRAY_MARGIN, since a very wide
        band can otherwise reach past anything the array could physically produce. The cap can never
        pull the ceiling below p90 itself - that would invert the upside scenario into a downside
        one, which is the same failure mode load_scaling90 is clamped against.
        """
        array_kwp = getattr(self, "pv_array_kwp", 0) or 0
        cap = (array_kwp * CLOUD_ARRAY_MARGIN / 60.0) if array_kwp > 0 else None

        ceiling = {}
        for minute in range(self.forecast_minutes):
            minute_absolute = minute + minutes_now
            upside = pv_data90.get(minute_absolute, 0.0)
            extended = upside + max(upside - pv_data.get(minute_absolute, 0.0), 0.0)
            if cap is not None:
                extended = max(min(extended, cap), upside)
            ceiling[minute_absolute] = extended
        return ceiling

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

    def inday_yesterday_weight(self, minutes_now):
        """
        Weight the previous day's in-day load adjustment carries at a point in the day: full weight for the
        first three hours (too little of today has happened to measure a divergence), then decaying linearly
        to zero by midnight as today's own measurement takes over.
        """
        if minutes_now < 180:
            return 1.0
        return (24 * 60 - minutes_now) / (24 * 60)

    def inday_adjustment_at(self, minute_absolute, scale_today):
        """
        In-day load adjustment factor to apply at a given minute from midnight today.

        Today keeps the factor in full. Tomorrow it decays on exactly the curve load_today_comparison() will
        use when it seeds tomorrow's factor from today's final value, so the plan agrees with what Predbat
        will actually apply a few hours later - resetting to 1.0 at midnight instead left tonight's overnight
        charge sized against a forecast Predbat was about to correct (batpred#4732). Beyond tomorrow there is
        no measurement left to carry and the factor is neutral.
        """
        day_offset = minute_absolute // (24 * 60)
        if day_offset <= 0:
            return scale_today
        if day_offset > 1:
            return 1.0
        return 1.0 + (scale_today - 1.0) * self.inday_yesterday_weight(minute_absolute % (24 * 60))

    def step_data_history(
        self,
        item,
        minutes_now,
        forward,
        step=PREDICT_STEP,
        scale_today=1.0,
        scale_fixed=1.0,
        type_load=False,
        load_forecast=None,
        cloud_factor=None,
        cloud_ceiling=None,
        cloud_duty=None,
        load_scaling_dynamic=None,
        base_offset=None,
        flip=False,
        load_adjust=None,
        load_baseline=None,
    ):
        """
        Create cached step data for historical array
        """
        if load_baseline is None:
            load_baseline = {}
        if load_adjust is None:
            load_adjust = {}
        if load_forecast is None:
            load_forecast = {}
        values = {}
        cloud_diff = 0

        for minute in range(0, self.forecast_minutes + self.plan_interval_minutes, step):
            value = 0
            minute_absolute = minute + minutes_now

            scaling_dynamic = 1.0
            if load_scaling_dynamic:
                scaling_dynamic = load_scaling_dynamic.get(minute_absolute, scaling_dynamic)

            # Carry the in-day adjustment over midnight on the decay curve load_today_comparison() will
            # apply tomorrow, rather than resetting it to 1.0 (batpred#4732)
            scale_slot = self.inday_adjustment_at(minute_absolute, scale_today)

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
                for _offset in range(step):
                    load_extra += self.get_from_incrementing(load_forecast, minute_absolute, backwards=False)
            if load_adjust:
                load_extra += load_adjust.get(minute_absolute, 0) * step / float(self.plan_interval_minutes)  # The kWh figure is for the plan interval period, so divide by plan_interval_minutes and times by step
            load_extra = max(load_extra, -value)  # Don't allow going to negative load values
            values[minute] = dp4((value + load_extra) * scaling_dynamic * scale_slot * scale_fixed)

            # Apply dynamic baseline
            if minute_absolute in load_baseline:
                values[minute] = max(values[minute], load_baseline[minute_absolute])

        # Envelope-driven divergence model. Where the legacy model below swings by a fraction of
        # the local value, this one swings toward a real per-minute ceiling - the next forecast
        # percentile up - so the amplitude follows what the forecaster actually said about
        # uncertainty at that time of day rather than one horizon-wide scalar.
        #
        # Conservation is per CLOUD_WINDOW_MINUTES rather than per adjacent pair. That is the whole
        # point of the window: a pair can only trade 1:1, so a trough can never fall further than
        # the peak beside it rose, and with an asymmetric p10/p50/p90 band the peak is the narrow
        # side. Six steps to a window allow an uneven duty cycle (n_up up, n_down down), which lets
        # peaks reach the ceiling while troughs still fall far enough to change the plan.
        if cloud_ceiling and cloud_duty:
            n_up, n_down = cloud_duty
            period = n_up + n_down
            # flip offsets the pattern by n_up so a flipped series lowers exactly where the plain
            # one raises - the antiphase the legacy model got from a 1:1 alternation, generalised
            # to an uneven duty cycle.
            phase = n_up if flip else 0
            for window_start in range(0, self.forecast_minutes, CLOUD_WINDOW_MINUTES):
                offsets = [minute for minute in range(window_start, min(window_start + CLOUD_WINDOW_MINUTES, self.forecast_minutes), step)]
                ups = [minute for minute in offsets if (((minute + minutes_now) // step) + phase) % period < n_up]
                downs = [minute for minute in offsets if minute not in ups]
                if not ups or not downs:
                    continue
                window_total = sum(values[minute] for minute in offsets)
                headroom = {minute: max(sum(cloud_ceiling.get(minute + minutes_now + offset, 0.0) for offset in range(step)) - values[minute], 0.0) for minute in ups}
                want = sum(headroom.values())
                # Each down step can give back at most what it holds, so the window can never be
                # driven negative and never has to leave a borrow outstanding.
                room = sum(max(values[minute] - (load_baseline.get(minute + minutes_now, 0.0) if load_baseline else 0.0), 0.0) for minute in downs)
                borrow = min(want, room)
                if borrow <= 0:
                    continue
                for minute in ups:
                    values[minute] = dp4(values[minute] + headroom[minute] * borrow / want)
                for minute in downs:
                    floor = load_baseline.get(minute + minutes_now, 0.0) if load_baseline else 0.0
                    values[minute] = dp4(values[minute] - borrow * max(values[minute] - floor, 0.0) / room)
                # dp4 on each step leaves up to half a milliwatt-hour per step unaccounted for.
                # Push that residual onto the roomiest down step, which recovers the window total on
                # any window holding meaningful energy - the scenarios are compared against each
                # other, so a systematic drift in one of them is a bias in the plan, not a rounding
                # detail. It is not guaranteed: the correction is floored at zero, so a window whose
                # roomiest down step holds less than the residual keeps the remainder. In practice
                # that is confined to near-empty dawn and dusk windows and measures around 0.001 kWh
                # over a 48 hour horizon.
                drift = sum(values[minute] for minute in offsets) - window_total
                if drift:
                    roomiest = max(downs, key=lambda minute: values[minute])
                    values[roomiest] = dp4(max(values[roomiest] - drift, 0.0))

        # Simple divergence model that raises and lowers alternate steps so the planner stops
        # assuming PV exactly blankets load. It is approximately - not exactly - total-preserving:
        # a raised step borrows, and the following step repays only what its own value can cover
        # (`min(cloud_diff, values[minute])`), so a borrow larger than the step that follows it is
        # not fully given back. Repaying exactly needs the outstanding debt tracked separately from
        # the next borrow, which is a rewrite of the model rather than a tweak here.
        if cloud_factor and cloud_factor > 0:
            last_minute = self.forecast_minutes - step
            for minute in range(0, self.forecast_minutes, step):
                # The inner parentheses matter: a conditional expression binds looser than "+", so
                # "int(...) + 1 if flip else 0" is "(int(...) + 1) if flip else 0" - which is a
                # constant 0 whenever flip is False, leaving every non-flipped caller unmodulated.
                # flip only means anything as an antiphase to a modulated base, so it is the offset
                # that is conditional, not the whole expression.
                #
                # Keyed off the minutes_now parameter, not self.minutes_now: every other minute in
                # this function comes from the parameter, and output.py already calls it with 0.
                # That also keeps the result a pure function of the inputs, which the replay and
                # test paths rely on for determinism.
                cloud_on = (int((minute + minutes_now) / step) + (1 if flip else 0)) % 2
                if cloud_on > 0:
                    # Don't borrow on the final step - the fill loop above runs a plan interval
                    # past forecast_minutes, so values.get(minute + step) finds a real bucket that
                    # this loop will never reach to repay, leaking that borrow into the total.
                    if minute < last_minute:
                        cloud_diff += min(values[minute] * cloud_factor, values.get(minute + step, 0) * cloud_factor)
                        values[minute] += cloud_diff
                else:
                    # The dynamic load baseline is a floor, applied in the fill loop above. The
                    # subtract has to respect it or the modulation walks straight back through it -
                    # only ever an issue since the load arrays started being modulated at all.
                    floor = load_baseline.get(minute + minutes_now, 0.0) if load_baseline else 0.0
                    subtract = min(cloud_diff, max(values[minute] - floor, 0.0))
                    values[minute] -= subtract
                    cloud_diff = 0
                values[minute] = dp4(values[minute])

        return values

    def get_filtered_load_window(self, data, indices, step):
        """
        Compute the car-charging/iBoost-filtered load for a single window given its list of backwards minute
        indices. Returns (filtered_load, raw_load) for that one window (no base-load applied).
        """
        raw = 0.0
        subtract_energy = 0.0
        for idx in indices:
            raw += self.get_from_incrementing(data, idx)
            if self.car_charging_hold and self.car_charging_energy:
                subtract_energy += self.get_from_incrementing(self.car_charging_energy, idx)
            if self.iboost_energy_subtract and self.iboost_energy_today:
                subtract_energy += self.get_from_incrementing(self.iboost_energy_today, idx)
        load = max(0.0, raw - subtract_energy)
        if self.car_charging_hold and (not self.car_charging_energy) and (load >= (self.car_charging_threshold * step)):
            # Car charging hold - ignore car charging in this window based on the threshold
            load = max(load - (self.car_charging_rate[0] * step / 60.0), 0)
        return load, raw

    def get_filtered_load_minute(self, data, minute_previous, historical, step=1, base_in_raw=True):
        """
        Gets a previous load minute after filtering for car charging.

        For historical (days_previous) data the car-charging hold and car/iBoost subtraction are applied to
        each previous day individually and only then weighted-averaged, so a single day's car charging is not
        diluted below the hold threshold by averaging it with the other (lower-load) days first.

        base_in_raw controls whether the base-load top-up is reflected in the returned raw value. Set it to
        False to keep the raw value at the true measured load so callers can detect genuine zero/gap buckets.
        """
        if historical:
            total = 0.0
            total_raw = 0.0
            total_weight = 0.0
            for point, days in enumerate(self.days_previous):
                use_days = max(min(days, self.load_minutes_age), 1)
                weight = self.days_previous_weight[point]
                full_days = 24 * 60 * (use_days - 1)
                indices = [24 * 60 - (minute_previous + offset) + full_days for offset in range(step)]
                day_load, day_raw = self.get_filtered_load_window(data, indices, step)
                total += day_load * weight
                total_raw += day_raw * weight
                total_weight += weight
            if total_weight > 0:
                load_yesterday = total / total_weight
                load_yesterday_raw = total_raw / total_weight
            else:
                load_yesterday = 0.0
                load_yesterday_raw = 0.0
        else:
            indices = [minute_previous + offset for offset in range(step)]
            load_yesterday, load_yesterday_raw = self.get_filtered_load_window(data, indices, step)

        # Apply base load
        base_load = self.base_load * step / 60.0
        if load_yesterday < base_load:
            add_to_base = base_load - load_yesterday
            load_yesterday += add_to_base
            if base_in_raw:
                load_yesterday_raw += add_to_base

        return load_yesterday, load_yesterday_raw

    def fill_load_from_power(self, load_minutes, load_power_data):
        """
        This function helps to deal with load sensors which don't increment very often leading to poor quality load data
        For example if its in kWh units then it might take many hours to increment.
        Predbat actually wants a much more real time estimate, but integrating power sensors alone leads to drift over time.

        Strategy: Divide data into 30-minute periods. For each period:
        1. Calculate total load consumed (difference between start and end of period)
        2. Integrate power data over that period
        3. Scale power data to match the total load
        4. Generate smooth minute-by-minute load curve

        Since data goes backwards in time (minute 0 is now, higher minutes are further in past),
        the load value DECREASES as we go forward in time (backwards through minutes).
        """

        if not load_power_data:
            self.log("Warn: No power data provided to fill_load_from_power")
            return load_minutes

        # Create a copy to avoid modifying the original
        new_load_minutes = load_minutes.copy()

        # Find all the minutes we have load data for.
        # Deliberately do NOT extend max_minute using load_power_data.keys() even if the power
        # sensor has older history than the load sensor.  Any minutes beyond the load data range
        # have no cumulative energy readings and would all default to 0, which the zero-period
        # detection incorrectly treats as a full-day data gap every run.
        max_minute = max(load_minutes.keys()) if load_minutes else 0

        # Determine gap size threshold for zero-load detection
        # This uses the same threshold as the gap filling logic
        gap_size = max(self.get_arg("load_filter_threshold", self.plan_interval_minutes), 5)

        # Preprocessing: Fill periods of zero load where power data exists
        # Find zero-load periods by looking for consecutive minutes with the same value (or 0)
        zero_periods = []
        period_start = None
        last_value = None

        for minute in range(0, max_minute + 1):
            current_value = new_load_minutes.get(minute, 0)

            # Check if this is a zero or constant period
            if minute == 0:
                last_value = current_value
                if current_value == 0 or current_value == new_load_minutes.get(minute + 1, 0):
                    period_start = minute
            else:
                # If value is same as previous and same as next, it's a flat period
                next_value = new_load_minutes.get(minute + 1, current_value)
                if current_value == last_value and current_value == next_value:
                    if period_start is None:
                        period_start = minute
                else:
                    # End of constant period
                    if period_start is not None and period_start < minute:
                        period_length = minute - period_start
                        # Only consider it zero load if it exceeds the gap_size threshold
                        if period_length >= gap_size:
                            # Check if there's power data in this period
                            has_power = any(load_power_data.get(m, 0) > 0 for m in range(period_start, minute))
                            if has_power and (last_value == 0 or last_value == new_load_minutes.get(minute, 0)):
                                zero_periods.append((period_start, minute - 1, last_value))
                    period_start = None
                last_value = current_value

        # Check final period
        if period_start is not None and period_start < max_minute:
            period_length = max_minute - period_start + 1
            # Only consider it zero load if it exceeds the gap_size threshold
            if period_length >= gap_size:
                has_power = any(load_power_data.get(m, 0) > 0 for m in range(period_start, max_minute + 1))
                if has_power and last_value == 0:
                    zero_periods.append((period_start, max_minute, last_value))

        # Fill zero periods with integrated power data
        if zero_periods:
            self.log("Warn: Found {} periods of zero load with power data, filling using power integration".format(len(zero_periods)))
            # Print the first 5 periods for debugging
            for _i, (period_start, period_end, base_value) in enumerate(zero_periods[:5]):
                start_timestamp = self.now_utc - timedelta(minutes=period_start)
                self.log("Zero load period starting at {} ({} minutes) for {} minutes with base value {}".format(start_timestamp.strftime(TIME_FORMAT), period_start, period_end - period_start + 1, base_value))

            for period_start, period_end, _base_value in zero_periods:
                # Integrate power data over this period
                # First calculate total energy consumed in this period
                total_energy = 0
                for minute in range(period_start, period_end + 1):
                    power = load_power_data.get(minute, 0)
                    total_energy += power / 60.0 / 1000.0

                amount_to_fill = 0
                for minute in range(period_end, period_start, -1):
                    power = max(load_power_data.get(minute, 0), 0)
                    energy = power / 60.0 / 1000.0
                    amount_to_fill += energy
                    new_load_minutes[minute] = new_load_minutes.get(minute, 0) + amount_to_fill
                # Fill to start
                for minute in range(period_start, -1, -1):
                    new_load_minutes[minute] = new_load_minutes.get(minute, 0) + amount_to_fill

        # Process in 30-minute periods
        period_length = 30
        num_periods = (max_minute + period_length) // period_length

        self.log("Processing {} 30-minute periods for power integration".format(num_periods))
        for period_idx in range(num_periods):
            period_start = period_idx * period_length
            period_end = min(period_start + period_length - 1, max_minute)

            # Get load at start and end of period (going backwards in time)
            # period_start is more recent (higher cumulative value)
            # period_end is further in past (lower cumulative value)
            load_at_start = new_load_minutes.get(period_start, 0)
            load_at_end = new_load_minutes.get(period_end + 1, new_load_minutes.get(period_end, 0))

            # Total energy consumed in this period (going backwards means decrement)
            load_total = load_at_start - load_at_end

            # Integrate power data over this period (convert W to kWh)
            integrated_energy = 0.0
            power_count = 0
            for minute in range(period_start, period_end + 1):
                power = max(load_power_data.get(minute, 0), 0)
                if power > 0:
                    power_count += 1
                # Convert watts to kWh per minute: W * (1 hour / 60 minutes) / 1000
                integrated_energy += power / 60.0 / 1000.0

            # If we have both power data and load consumption, scale the power integration
            if integrated_energy > 0 and load_total > 0:
                # Calculate scaling factor to match load total
                scale_factor = load_total / integrated_energy

                # Apply scaled power integration to create smooth load curve
                # Start from the highest value (at period_start = most recent)
                # and decrement going backwards in time
                running_total = load_at_start
                for minute in range(period_start, period_end + 1):
                    new_load_minutes[minute] = dp4(running_total)
                    power = max(load_power_data.get(minute, 0), 0)
                    energy_decrement = (power / 60.0 / 1000.0) * scale_factor
                    running_total -= energy_decrement
            elif load_total > 0:
                # No power data but we have load consumption
                # Distribute the load evenly across the period
                energy_per_minute = load_total / (period_end - period_start + 1)
                running_total = load_at_start
                for minute in range(period_start, period_end + 1):
                    new_load_minutes[minute] = dp4(running_total)
                    running_total -= energy_per_minute

        return new_load_minutes

    def fill_load_from_history(self, load_minutes, load_minutes_age):
        """
        Fill the days before load_today's history starts from the history of Predbat's own load_energy_h0 sensor.

        load_energy_h0 republishes today's load (unscaled, unfiltered, summed across inverters) every cycle
        under an entity id that never changes, so its history outlives a change of load_today sensor, such as a
        new inverter. Where load_today is flat from the oldest minute back for at least a day (the sensor did
        not exist yet), the increments load_energy_h0 recorded over that stretch are put in its place.

        Flat stretches inside load_today's own history are left for fill_load_from_power and the average gap
        filler. There load_energy_h0 was published from this same sensor, so it adds nothing true: it holds
        what load_today counted, a cycle late, so smoothing spreads each step back into the flat before it,
        plus whatever this fill added the cycle before, so the fill fed on its own output and today's load
        climbed every cycle (#5652). History backends that keep only the state cannot say which sensor a
        record came from, so the stretch it was measured over is the only safe test. Records tagged as
        published from load_forecast alone are skipped, as they are not measured.

        Returns (load_minutes, load_minutes_age), with the age extended over any older days filled in.
        """
        if not self.get_arg("load_history_fill_enable", True):
            return load_minutes, load_minutes_age

        size = self.max_days_previous * 24 * 60 + 2
        data = load_minutes.copy() if load_minutes else MinuteArray({}, size)
        max_minute = (len(data) - 1) if isinstance(data, MinuteArray) else max(data.keys())
        gap_size = max(self.get_arg("load_filter_threshold", self.plan_interval_minutes), 5)

        # Find where load_today's history starts: the newest minute of the flat run back to the oldest minute.
        # Under a day is not a missing sensor - a history that only lists changes can open hours into the
        # window on a quiet night - and load_energy_h0 then came from this same sensor
        gap_start = max_minute
        while gap_start > 0 and data.get(gap_start - 1, 0) == data.get(max_minute, 0):
            gap_start -= 1
        if max_minute - gap_start < 24 * 60:
            return load_minutes, load_minutes_age

        entity_id = self.prefix + ".load_energy_h0"
        history = self.get_history_wrapper(entity_id, days=self.max_days_previous, required=False)
        if not (isinstance(history, list) and history and history[0]):
            return load_minutes, load_minutes_age
        # Keep the measured records, and note where consecutive ones are too far apart to join up: the smoothing
        # below would otherwise spread the difference between them across a stretch nobody measured, whether
        # Predbat was not running or was publishing a forecast. Those stretches are left to the later fillers.
        records = []
        unknown = []
        previous_minute = None
        bridged_forecast = False
        try:
            for record in sorted(history[0], key=lambda record: str2time(record["last_updated"])):
                record_minute = int((self.now_utc - str2time(record["last_updated"])).total_seconds() / 60)
                if (record.get("attributes") or {}).get("source") == "load_forecast":
                    bridged_forecast = True
                    continue
                if previous_minute is not None and (bridged_forecast or previous_minute - record_minute > gap_size):
                    unknown.append((max(record_minute, 0), previous_minute))
                records.append(record)
                previous_minute = record_minute
                bridged_forecast = False
        except (ValueError, TypeError, KeyError):
            return load_minutes, load_minutes_age
        if not records:
            return load_minutes, load_minutes_age

        history_age = (self.now_utc - str2time(records[0]["last_updated"])).days
        # No extrapolation past the newest record: load_energy_h0 is republished every cycle, so extrapolating
        # it would add the last rate seen on top of what was published from the last fill, cycle after cycle
        history_minutes, _ = minute_data(records, self.max_days_previous, self.now_utc, "state", "last_updated", backwards=True, smoothing=True, clean_increment=True, required_unit="kWh", interpolate=False)

        # Mark the minutes before load_today's history that load_energy_h0 measured, then walk from oldest to
        # newest carrying the energy added so far forward, so the data keeps incrementing all the way to minute 0
        in_gap = bytearray(max_minute + 1)
        for minute in range(gap_start, max_minute):
            in_gap[minute] = 1
        for newest, oldest_minute in unknown:
            for minute in range(newest, min(oldest_minute, max_minute + 1)):
                in_gap[minute] = 0
        added = 0.0
        for minute in range(max_minute, -1, -1):
            if in_gap[minute]:
                added += max(history_minutes.get(minute, 0) - history_minutes.get(minute + 1, 0), 0)
            if added:
                data[minute] = dp4(data.get(minute, 0) + added)

        if added <= 0:
            return load_minutes, load_minutes_age

        # Days older than load_today's own history are now real data, so extend the age to cover them
        if history_minutes.get(gap_start, 0) > history_minutes.get(max_minute, 0):
            load_minutes_age = min(max(load_minutes_age, history_age), self.max_days_previous)

        self.log("Filled load_today before its history starts {} minutes ago with {}kWh from {} history, data now goes back {} days".format(gap_start, dp2(added), entity_id, load_minutes_age))
        return data, load_minutes_age

    def fetch_load_today(self):
        """
        Read the load_today history, backfill the days before it starts from load_energy_h0, then work out
        today's load so far and the last period's load from the filled data.
        """
        load_minutes, load_minutes_age = self.minute_data_load(self.now_utc, "load_today", self.max_days_previous, required_unit="kWh", load_scaling=1.0, interpolate=True)
        self.log("Found {} load_today datapoints going back {} days".format(len(load_minutes), load_minutes_age))
        self.load_minutes, self.load_minutes_age = self.fill_load_from_history(load_minutes, load_minutes_age)
        self.load_minutes_source = "load_today"
        self.load_minutes_now = get_now_from_cumulative(self.load_minutes, self.minutes_now, backwards=True)
        self.load_last_period = (self.load_minutes.get(0, 0) - self.load_minutes.get(PREDICT_STEP, 0)) * 60 / PREDICT_STEP

    def previous_days_modal_filter(self, data):
        """
        Look at the data from previous days and discard the best case one
        """

        total_points = len(self.days_previous)
        sum_days = []
        sum_days_id = {}
        min_sum = 99999999
        min_sum_day = 0
        sum_all_days = {}

        days_list = self.days_previous.copy()
        # Sort days list in numerical order with highest number day first
        days_list.sort(reverse=True)
        max_days = min(max(days_list), self.load_minutes_age) if self.load_minutes_age > 0 else max(days_list)
        for days in range(1, max_days + 1):
            sum_day = 0
            full_days = 24 * 60 * (days - 1)
            for minute in range(0, 24 * 60, PREDICT_STEP):
                minute_previous = 24 * 60 - minute + full_days - 1
                load_yesterday, load_yesterday_raw = self.get_filtered_load_minute(data, minute_previous, historical=False, step=PREDICT_STEP)
                sum_day += load_yesterday
            if days in days_list:
                sum_days.append(dp2(sum_day))
                sum_days_id[days] = sum_day
                if sum_day < min_sum:
                    min_sum_day = days
                    min_sum = dp2(sum_day)
            sum_all_days[days] = dp2(sum_day)

        # Work out the average non-zero day
        average_non_zero_day = 0
        average_non_zero_count = 0
        for day_sum in sum_all_days.values():
            # Sensible threshold to ignore zero days
            if day_sum > 2.5:
                average_non_zero_day += day_sum
                average_non_zero_count += 1
        if average_non_zero_count > 0:
            average_non_zero_day /= average_non_zero_count
        else:
            average_non_zero_day = 24  # Assume a nominal 24kWh day if no data

        # log the unsorted list of days and load kWh, easier to read and ensures correct 'lowest day' is reported
        self.log("Historical load totals for days {} are {}kWh, minimum value {}kWh".format(self.days_previous.copy(), sum_days, min_sum))
        if self.load_filter_modal and total_points >= 3 and (min_sum_day > 0):
            self.log("Modal filter enabled - Discarding day {} as it is the lowest of the {} datapoints".format(min_sum_day, len(days_list)))
            min_sum_day_idx = days_list.index(min_sum_day)
            del days_list[min_sum_day_idx]
            # Remove day 'min_sum_day' from self.days_previous
            min_sum_day_idx = self.days_previous.index(min_sum_day)
            del self.days_previous[min_sum_day_idx]
            del self.days_previous_weight[min_sum_day_idx]

        # Fill all the gaps including days we don't use
        gap_size = max(self.get_arg("load_filter_threshold", self.plan_interval_minutes), 5)
        gap_minutes = 0
        gap_start_minute_previous = None
        gap_list = []
        num_gaps = 0
        max_minute = max(max_days * 24 * 60, max(data.keys()) if data else 0)

        # Find all the gaps
        for minute_previous in range(0, max_minute, PREDICT_STEP):
            if data.get(minute_previous, 0) == data.get(minute_previous + PREDICT_STEP, 0):
                gap_minutes += PREDICT_STEP
                if gap_start_minute_previous is None:
                    gap_start_minute_previous = minute_previous
            else:
                if gap_minutes >= gap_size:
                    num_gaps += gap_minutes
                    gap_list.append((gap_start_minute_previous, gap_minutes))
                gap_minutes = 0
                gap_start_minute_previous = None
        if gap_minutes >= gap_size:
            num_gaps += gap_minutes
            gap_list.append((gap_start_minute_previous, gap_minutes))

        # Work out total number of gap_minutes
        if num_gaps > 0:
            self.log("Warn: Found {} gaps in load_today totalling {} minutes to fill using average data".format(len(gap_list), num_gaps))
            # Print the first 5 gaps for debugging
            for _i, (gap_start, gap_length) in enumerate(gap_list[:5]):
                gap_start_timestamp = self.now_utc - timedelta(minutes=gap_start)
                self.log("Gap starting at {} ({} minutes) for {} minutes".format(gap_start_timestamp.strftime(TIME_FORMAT), gap_start, gap_length))

        # Do the filling
        len_data = len(data) if isinstance(data, MinuteArray) else 99999999
        for gap in gap_list:
            gap_start_minute_previous = gap[0]
            gap_minutes = gap[1]
            gap_end_minute_previous = gap_start_minute_previous + gap_minutes

            total_to_add = 0
            # Fill the gap region by stepping backward through time (decrementing minute_previous)
            # gap_start_minute_previous is the highest index (earliest in gap)
            # We fill from there down to the end of the gap

            minute_previous = min(gap_end_minute_previous, len_data - 1)
            gap_day = None
            while minute_previous > gap_start_minute_previous and minute_previous >= 0:
                # Change of day?
                new_gap_day = max(minute_previous // (24 * 60), 1)
                if gap_day is None or (new_gap_day != gap_day):
                    gap_day = new_gap_day
                    average_day = sum_all_days.get(gap_day, average_non_zero_day)
                    if average_day <= 2.5:
                        average_day = average_non_zero_day
                    per_minute_increment = average_day / (24 * 60)
                    gap_day = new_gap_day

                data[minute_previous] = dp4(data.get(minute_previous, 0) + total_to_add)
                minute_previous -= 1
                total_to_add += per_minute_increment

            # Now ensure the sensor is incrementing all the way to 0
            while minute_previous >= 0:
                data[minute_previous] = dp4(data.get(minute_previous, 0) + total_to_add)
                minute_previous -= 1

        return data

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

    def minute_data_import_export(self, max_days_previous, now_utc, key, scale=1.0, required_unit=None, increment=True, smoothing=True, pad=True, required=True):
        """
        Download one or more entities for import/export data

        :param required: Set False when the data only improves the result rather than being needed for it,
                         e.g. the rate history the ML load model adds as a training feature. A missing
                         history is then reported once as information instead of as a fetch failure, as
                         Home Assistant legitimately has no history for entities its recorder isn't storing.
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
            # Ignore invalid entity IDs, they might be fixed values
            if (not entity_id) or ("." not in entity_id):
                continue

            try:
                history = self.get_history_wrapper(entity_id=entity_id, days=max_days_previous, required=required)
            except (ValueError, TypeError) as exc:
                self.log("Warn: No history data found for {} : {}".format(entity_id, exc))
                history = []

            if history and len(history) > 0 and len(history[0]) > 0:
                item = history[0][0]
                try:
                    last_updated_time = str2time(item["last_updated"])
                except (ValueError, TypeError):
                    last_updated_time = now_utc
                age_days = max_days_previous
                age = now_utc - last_updated_time
                if age_days is None:
                    age_days = age.days
                else:
                    age_days = min(age_days, age.days)

                import_today, _ = minute_data(
                    history[0],
                    max_days_previous if pad else age_days,
                    now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    smoothing=smoothing,
                    scale=scale,
                    clean_increment=increment,
                    accumulate=import_today,
                    required_unit=required_unit,
                    can_modify_history=True,  # history is not accessed after this point, so minute_data can freely modify it
                )
            else:
                if not required:
                    # Optional data - Home Assistant simply has no history for this entity, which is
                    # normal when its recorder isn't configured to store it, so don't cry wolf
                    self.log("Info: No history available for {}, continuing without it".format(entity_id))
                elif history is None:
                    # Only record as a failure if it was None (not just empty but failure)
                    self.log("Warn: Failure to fetch history for {}".format(entity_id))
                    self.record_status("Warn: Failure to fetch history from {}".format(entity_id), had_errors=True)
                else:
                    self.log("Warn: Unable to fetch history for {}".format(entity_id))

        if import_today and smoothing:
            size = (max_days_previous * 24 * 60 + 2) if pad else (max(import_today.keys()) + 2)
            return MinuteArray(import_today, size)
        return import_today

    def minute_data_load(self, now_utc, entity_name, max_days_previous, load_scaling=1.0, required_unit=None, interpolate=False, pad=True, clean_increment=True):
        """
        Download one or more entities for load data
        """
        if "." not in entity_name:
            entity_ids = self.get_arg(entity_name, indirect=False)
        else:
            entity_ids = entity_name

        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        if entity_ids is None:
            self.log("Error: No entity IDs provided for {}".format(entity_name))
            entity_ids = []

        load_minutes = {}
        age_days = None
        for entity_id in entity_ids:
            if (not entity_id) or ("." not in entity_id):
                # Invalid entity ID, might be a fixed value
                continue

            try:
                history = self.get_history_wrapper(entity_id=entity_id, days=max_days_previous)
            except (ValueError, TypeError):
                history = []

            if isinstance(history, list) and history and history[0]:
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
                load_minutes, _ = minute_data(
                    history[0],
                    max_days_previous if pad else age_days,
                    now_utc,
                    "state",
                    "last_updated",
                    backwards=True,
                    smoothing=True,
                    scale=load_scaling,
                    clean_increment=clean_increment,
                    accumulate=load_minutes,
                    required_unit=required_unit,
                    interpolate=interpolate,
                    can_modify_history=True,  # history is not accessed after this point, so minute_data can freely modify it
                )
            else:
                if history is None:
                    # Only record as a failure if it was None (not just empty but failure)
                    self.log("Warn: Failure to fetch history for {}".format(entity_id))
                    self.record_status("Warn: Failure to fetch history from {}".format(entity_id), had_errors=True)
                else:
                    self.log("Warn: Unable to fetch history for {}".format(entity_id))

        if age_days is None:
            age_days = 0
        if load_minutes:
            size = (max_days_previous * 24 * 60 + 2) if pad else (max(load_minutes.keys()) + 2)
            load_minutes = MinuteArray(load_minutes, size)
        return load_minutes, age_days

    def log_replay_changed(self, attribute, signature_of, build, what):
        """Log the line build() returns when signature_of() differs from the signature last logged under attribute.

        The shared core of the "Replay input:" lines that are logged only on a change. The signature is recorded
        before the line is built, so a value that cannot be logged warns once per change rather than every cycle,
        and is logged again when it next changes. Both are computed inside the try, as either can fail on bad data.
        Never raises - these run every cycle on the main loop, and a logging fault must not stop the plan.
        """
        try:
            signature = signature_of()
            if signature == getattr(self, attribute, None):
                return
            setattr(self, attribute, signature)
            self.log(build())
        except Exception as e:
            self.log("Warn: Unable to log the {} for replay: {}".format(what, e))

    def replay_clock(self, minute):
        """A minute from midnight as HH:MM, for the start of a "Replay input:" line."""
        return (self.midnight_utc + timedelta(minutes=minute)).strftime("%H:%M")

    def log_replay_pv_forecast(self):
        """Log the PV forecast whenever it changes, so a log can be replayed against the forecast the plan really used.

        A debug yaml holds the forecast at one moment, and Solcast refreshes it during the day. step_data_history()
        sums every minute of the forecast across the plan, so p50, p10 and p90 are each logged per minute, exactly,
        as runs of [kWh per minute, minutes] (None where the forecast has no value). The line is written only on a
        change, while the plan's horizon moves on with the clock, so it covers from now to the end of the forecast
        rather than to the end of today's plan. One line per change. Never raises - this runs every cycle on the main loop, and a logging fault must
        not stop the plan.
        """
        series = (self.pv_forecast_minute or {}, self.pv_forecast_minute10 or {}, self.pv_forecast_minute90 or {})

        def build():
            """The PV forecast line, from now to the end of the forecast."""
            start = self.minutes_now
            end = max([start + self.forecast_minutes + self.plan_interval_minutes] + [max(values) + 1 for values in series if values])
            parts = []
            for name, values in zip(("p50", "p10", "p90"), series):
                runs = []
                for minute in range(start, end):
                    value = values.get(minute)
                    value = None if value is None else float(value)
                    if runs and runs[-1][0] == value:
                        runs[-1][1] += 1
                    else:
                        runs.append([value, 1])
                parts.append("{} [{}]".format(name, ", ".join("[{}, {}]".format(None if value is None else repr(value), count) for value, count in runs)))
            return "Replay input: PV forecast changed, per-minute kWh runs from {} {}".format(self.replay_clock(start), " ".join(parts))

        self.log_replay_changed("replay_pv_signature", lambda: hash(tuple(tuple(sorted(values.items())) for values in series)), build, "PV forecast")

    def log_replay_ml_forecast(self, results):
        """Log the Load ML predictions whenever they change, so a log can be replayed against them.

        The predictions come from a model retrained in the background, so a replay cannot re-derive them; they are
        an input, like the PV forecast. They are logged as read from the sensor, before Predbat converts and
        re-anchors them, so that code still runs in a replay. Evenly spaced timestamps are logged as a start, a step
        and the values; anything else as the full mapping. Never raises - this runs every cycle on the main loop.
        """
        if not isinstance(results, dict):
            return
        items = []

        def signature_of():
            """Sort the predictions (inside the logger's try, as mixed keys cannot be sorted) and hash them."""
            items[:] = sorted(results.items())
            return hash(tuple((str(key), str(value)) for key, value in items))

        def build():
            """The ML forecast line: a start, a step and the values when the timestamps are evenly spaced, else the mapping."""
            try:
                times = [str2time(str(key)) for key, _ in items]
                steps = {int((after - before).total_seconds() // 60) for before, after in zip(times, times[1:])}
            except (ValueError, TypeError):
                steps = set()
            if items and len(steps) == 1:
                return "Replay input: ML load forecast changed, from {} every {} minutes kWh [{}]".format(items[0][0], steps.pop(), ", ".join(repr(float(value)) for _, value in items))
            return "Replay input: ML load forecast changed, kWh {!r}".format(dict(items))

        self.log_replay_changed("replay_ml_signature", signature_of, build, "ML load forecast")

    def log_replay_load_forecast(self):
        """Log the load forecast the plan will use this cycle, so a log can be replayed against it.

        The forecast is rebuilt every cycle from a history that the load_power fill re-cuts each time, so it cannot
        be reproduced from a debug yaml. step_data_history() reads load_forecast at the start of each 5-minute step
        and the minute after it, so those two cumulative-from-midnight values are what the line must reproduce, for
        every step the plan builds. One line per cycle. Never raises - this runs every cycle on the main loop.

        The forecasts are rounded to 0.1 Wh (dp4 kWh), so they are logged compactly as whole tenths of a Wh: the
        cumulative value at the first step, then for each step "energy in the 5 minutes/energy in its first minute"
        in Wh. The replay rebuilds each value as tenths / 10000, which is exactly the float the plan used. Where any
        value is missing or not a whole tenth of a Wh (another forecast source), the cumulative kWh values are logged
        in full instead.
        """
        try:
            if not self.load_forecast:
                # The plan then uses the load history alone, so a replay must drop any forecast it still holds.
                # Logged when the forecast becomes empty, not every cycle, as many setups have no forecast at all.
                self.log_replay_changed("replay_load_empty", lambda: True, lambda: "Replay input: load forecast empty", "load forecast")
                return
            self.replay_load_empty = None
            start = self.minutes_now
            steps = list(range(start, start + self.forecast_minutes + self.plan_interval_minutes, PREDICT_STEP))
            # The value at each step, the minute after it, and the start of the step after the last
            minutes = sorted(set(steps + [minute + 1 for minute in steps] + [steps[-1] + PREDICT_STEP]))
            values = {minute: self.load_forecast.get(minute) for minute in minutes}
            tenths = {}
            for minute, value in values.items():
                if value is None or round(value * 10000) / 10000 != value:
                    tenths = None
                    break
                tenths[minute] = round(value * 10000)
            when = self.replay_clock(start)
            if tenths is not None:

                def wh(count):
                    """A count of tenths of a Wh as Wh text that reads back to the same count."""
                    return "{:.1f}".format(count / 10)

                parts = ["{}/{}".format(wh(tenths[minute + PREDICT_STEP] - tenths[minute]), wh(tenths[minute + 1] - tenths[minute])) for minute in steps]
                self.log("Replay input: load from {} base {} Wh, Wh/5min [{}]".format(when, wh(tenths[start]), ", ".join(parts)))
                return

            def exact(minute):
                """The forecast's value at minute as text that reads back as the same float, or None."""
                value = self.load_forecast.get(minute)
                return None if value is None else repr(float(value))

            pairs = ["[{}, {}]".format(exact(minute), exact(minute + 1)) for minute in steps]
            self.log("Replay input: load forecast, cumulative kWh at each 5-minute step and the minute after from {} [{}]".format(when, ", ".join(pairs)))
        except Exception as e:
            self.log("Warn: Unable to log the load forecast for replay: {}".format(e))

    def log_replay_rates(self):
        """Log the import and export rates, and the base curves beside them, whenever they change.

        Rates arrive from outside after a debug yaml is written - the next day's Agile prices, a new futurerate
        prediction - so a replay needs them as an input. Each series is logged from now as [minutes from now, rate]
        at every change of rate, followed by the minute it runs to, at full precision. Never raises - this runs
        every cycle on the main loop.
        """
        series = (("import", self.rate_import), ("export", self.rate_export), ("import_base", self.rate_import_base), ("export_base", self.rate_export_base))

        def build():
            """The rates line: each series as change points from now, then the minute it runs to."""
            start = self.minutes_now
            parts = []
            for name, rates in series:
                points = []
                last = None
                minutes = sorted(minute for minute in (rates or {}) if minute >= start)
                for minute in minutes:
                    if rates[minute] != last:
                        last = rates[minute]
                        points.append([minute - start, last])
                parts.append("{} {} {}".format(name, points, minutes[-1] + 1 - start if minutes else 0))
            return "Replay input: rates changed, from {} {}".format(self.replay_clock(start), " ".join(parts))

        self.log_replay_changed("replay_rates_signature", lambda: hash(tuple(tuple(sorted((rates or {}).items())) for _, rates in series)), build, "rates")

    def log_replay_inverter(self):
        """Log the inverter's programmed state whenever it changes: the windows it holds now, whether it is charging or
        exporting and to what target, the reserve and the battery rate limits.

        The plan treats a window the inverter is already running specially, and the prediction starts from the rates
        and reserve in force, so a replay needs them as they were. Logged as one dict whose repr reads back exactly with
        ast.literal_eval. Never raises - this runs every cycle on the main loop.
        """
        self.log_replay_fields("inverter", REPLAY_INVERTER_FIELDS, "replay_inverter_text")

    def log_replay_cars(self):
        """Log the car state the plan reads whenever it changes, so a log can be replayed across a car plugging in.

        Plugging in, an Octopus Intelligent dispatch plan or a SoC reading all change the car's load in the plan,
        and a debug yaml holds them at one moment only. The per-car lists are logged as one dict whose repr reads
        back exactly with ast.literal_eval. Never raises - this runs every cycle on the main loop.
        """
        if not self.num_cars:
            return
        self.log_replay_fields("cars", REPLAY_CAR_FIELDS, "replay_cars_text")

    def log_replay_fields(self, label, fields, attribute):
        """Log fields as one "Replay input: <label> changed" dict, on a change, checking that its repr reads back."""
        state = {name: getattr(self, name, None) for name in fields}

        def build():
            """The line, once its dict is known to read back exactly with ast.literal_eval."""
            text = repr(state)
            ast.literal_eval(text)
            return "Replay input: {} changed {}".format(label, text)

        self.log_replay_changed(attribute, lambda: repr(state), build, label + " state")

    def log_replay_baseline(self):
        """Log the dynamic load baseline the plan will use, when it changes.

        dynamic_load() raises the load in the current slot or two to the load just measured when it is high, and
        step_data_history() adds that floor to all three load scenarios, so a replay needs it as it was. Usually
        empty. Logged as a dict of minute to kWh whose repr reads back exactly.
        """
        baseline = dict(self.dynamic_load_baseline or {})
        self.log_replay_changed("replay_baseline_text", lambda: repr(baseline), lambda: "Replay input: dynamic load baseline {!r}".format(baseline), "dynamic load baseline")

    def log_replay_state(self):
        """Log the values the plan starts from this cycle at full precision.

        The human-readable lines round them (SoC to 0.01 kWh, the in-day adjustment to 0.01%), and that rounding is
        enough to flip a plan that is within a fraction of a penny of another. Never raises - this runs every cycle
        on the main loop.
        """
        try:

            def exact(value):
                """Shortest text that reads back as the same float, or None."""
                return None if value is None else repr(float(value))

            self.log(
                "Replay input: state soc_kw {} soc_max {} inday {} cost_today {} load_today {} import_today {} export_today {} pv_today {} charge_rate_now {} discharge_rate_now {} battery_temperature {} iboost_today {} load_forecast_only {}".format(
                    exact(self.soc_kw),
                    exact(self.soc_max),
                    exact(self.load_inday_adjustment),
                    exact(self.cost_today_sofar),
                    exact(self.load_minutes_now),
                    exact(self.import_today_now),
                    exact(self.export_today_now),
                    exact(self.pv_today_now),
                    exact(self.charge_rate_now),
                    exact(self.discharge_rate_now),
                    exact(self.battery_temperature),
                    exact(self.iboost_today),
                    exact(self.load_forecast_only),
                )
            )
        except Exception as e:
            self.log("Warn: Unable to log the state for replay: {}".format(e))

    def fetch_pv_forecast_and_dawn(self, save=True):
        """
        Fetch the PV forecast, compute the dawn light/dark split from it, and publish the dawn
        binary_sensor - as one call so the ordering that #4699 regressed on (the split must be
        computed from a freshly-fetched forecast, not the one left over from the previous cycle)
        can't drift apart again. Published unconditionally regardless
        of whether import rates currently exist, so the sensor never goes stale.

        Returns:
            pv_light_dark: dict as returned by calc_pv_light_dark(), also stored on self.pv_light_dark.
        """
        self.pv_forecast_minute, self.pv_forecast_minute10, self.pv_forecast_minute90 = self.fetch_pv_forecast()
        # Not for a tariff comparison run (save=False), whose forecast is not the one the live plan uses
        if save:
            self.log_replay_pv_forecast()

        # Stored on self, not just local, so it can be used elsewhere rather than only by the
        # window split below.
        pv_light_dark = self.pv_light_dark = self.calc_pv_light_dark()
        # "on" past dawn, "off" before it or when unclassified (no PV forecast) - based on the PV
        # forecast crossing low_power_pv_threshold_w, not the same as PV actually producing right
        # now, see calc_dawn's docstring. Independent of combine_charge_slots/set_charge_low_power.
        self.dashboard_item(
            "binary_sensor." + self.prefix + "_dawn",
            state="on" if pv_light_dark.get(self.minutes_now) == 1 else "off",
            attributes={"friendly_name": "Predbat is past dawn (light, not dark, by PV forecast)", "icon": "mdi:weather-sunset-up"},
        )
        return pv_light_dark

    def combine_active_keep(self):
        """
        Combine the SOC keep floors (alerts, manual_soc) and ceilings (manual_soc_max) into all_active_keep/all_active_keep_max.
        """
        # Combine keep from alerts and manual SOC into all_active_keep
        self.all_active_keep = self.alert_active_keep.copy()
        if self.manual_soc_keep:
            for minute, soc_value in self.manual_soc_keep.items():
                if minute in self.all_active_keep:
                    self.all_active_keep[minute] = max(self.all_active_keep[minute], soc_value)
                else:
                    self.all_active_keep[minute] = soc_value

        # Manual SOC max is a ceiling rather than a floor - combine separately, taking the
        # tightest (lowest) ceiling if more than one source ever applies to the same minute.
        self.all_active_keep_max = {}
        if self.manual_soc_max_keep:
            for minute, soc_value in self.manual_soc_max_keep.items():
                if minute in self.all_active_keep_max:
                    self.all_active_keep_max[minute] = min(self.all_active_keep_max[minute], soc_value)
                else:
                    self.all_active_keep_max[minute] = soc_value

        # A ceiling below the floor at the same minute is a contradiction (e.g. a leftover manual_soc
        # override never cleared) - the floor wins as the safety-relevant constraint, so drop the
        # conflicting ceiling rather than hand the optimiser two penalties pulling opposite ways.
        for minute in list(self.all_active_keep_max.keys()):
            floor_value = self.all_active_keep.get(minute, 0)
            if floor_value > self.all_active_keep_max[minute]:
                self.log("Warn: manual_soc_max target {}% at minute {} is below the manual_soc/alert floor {}% for the same minute - ignoring the ceiling there".format(self.all_active_keep_max[minute], minute, floor_value))
                del self.all_active_keep_max[minute]

    def fetch_sensor_data(self, save=True):
        """
        Fetch all the data, e.g. energy rates, load, PV predictions, car plan etc.
        """

        prev_octopus_slots = self.octopus_slots.copy()
        prev_octopus_saving_slots = self.octopus_saving_slots.copy()
        prev_octopus_free_slots = self.octopus_free_slots.copy()
        prev_axle_sessions = self.axle_sessions.copy()

        import_rates = {}
        self.rate_import_replicated = {}
        self.rate_import_saving_minutes = set()
        self.rate_import_pre_saving = {}
        export_rates = {}
        self.rate_export_replicated = {}
        self.rate_export_saving_minutes = set()
        self.rate_export_pre_saving = {}
        self.rate_slots = []
        self.io_adjusted = {}
        self.low_rates = []
        self.low_rates_tariff = []
        self.high_export_rates = []
        self.octopus_slots = [[] for _ in range(self.num_cars)]
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
        # The PV forecasts are not reset here: fetch_pv_forecast_and_dawn() below replaces all three
        # at once, and until then a debug dump taken mid-fetch keeps last cycle's forecast rather
        # than capturing it empty (#4317)
        self.pv_light_dark = {}
        # See Plan.refresh_pv_forecast_minute90(): both series are re-fetched together below, so no
        # earlier pair of signatures may be held against them
        self.pv_forecast_minute90_signatures = None
        self.load_scaling_dynamic = {}
        self.carbon_intensity = {}
        self.carbon_history = {}
        curr = self.currency_symbols[1]

        # Alert feed if enabled
        if self.components:
            alert_feed = self.components.get_component("alert_feed")
            if alert_feed:
                self.alerts, self.alert_active_keep = alert_feed.process_alerts(self.minutes_now, self.midnight_utc)

        # Combine keep floors and ceilings from alerts and manual SOC
        self.combine_active_keep()

        # iBoost load data
        if "iboost_energy_today" in self.args:
            self.iboost_energy_today, iboost_energy_age = self.minute_data_load(self.now_utc, "iboost_energy_today", self.max_days_previous, required_unit="kWh", load_scaling=1.0)
            if iboost_energy_age >= 1:
                self.iboost_today = dp2(abs(self.iboost_energy_today[0] - self.iboost_energy_today[self.minutes_now]))
                self.log("iBoost energy today from sensor reads {} kWh".format(self.iboost_today))

        # Fetch ML forecast if enabled
        load_ml_forecast = {}
        if self.get_arg("load_ml_enable", False) and self.get_arg("load_ml_source", False):
            load_ml_forecast = self.fetch_ml_load_forecast(self.now_utc, save=save)
            if load_ml_forecast:
                self.load_forecast_only = True  # Use only ML forecast for load if enabled and we have data

        # Fetch extra load forecast
        self.load_forecast, self.load_forecast_array = self.fetch_extra_load_forecast(self.now_utc, load_ml_forecast)

        # Load previous load data
        if self.get_arg("ge_cloud_data", False):
            self.download_ge_data(self.now_utc)

            if ("load_power" in self.args) and self.get_arg("load_power_fill_enable", True):
                self.log("Using load_power data to fill gaps in load_today data")
                load_power_data, _ = self.minute_data_load(self.now_utc, "load_power", self.max_days_previous, required_unit="W", load_scaling=1.0, interpolate=True, clean_increment=False)
                self.load_minutes = self.fill_load_from_power(self.load_minutes, load_power_data)
        else:
            # Load data
            if "load_today" in self.args:
                self.fetch_load_today()

                if ("load_power" in self.args) and self.get_arg("load_power_fill_enable", True):
                    # Use power data to make load data more accurate.
                    # clean_increment=False: power sensors report instantaneous W, not cumulative kWh.
                    # clean_incrementing_reverse would distort fluctuating power readings into an
                    # ever-growing cumulative series, inflating fill_load_from_power gap-fills.
                    self.log("Using load_power data to fill gaps in load_today data")
                    load_power_data, _ = self.minute_data_load(self.now_utc, "load_power", self.max_days_previous, required_unit="W", load_scaling=1.0, interpolate=True, clean_increment=False)
                    self.load_minutes = self.fill_load_from_power(self.load_minutes, load_power_data)
            else:
                if self.load_forecast:
                    self.log("Using load forecast from load_forecast sensor")
                    self.load_minutes_now = self.load_forecast.get(0, 0)
                    self.load_minutes_source = "load_forecast"
                    self.load_minutes_age = 0
                    self.load_last_period = 0
                else:
                    self.log("Error: You have not set load_today or load_forecast in apps.yaml, you will have no load data")
                    self.record_status(message="Error: load_today not set correctly", had_errors=True)
                    raise ValueError

            # Load import today data
            if "import_today" in self.args:
                self.import_today = self.minute_data_import_export(self.max_days_previous, self.now_utc, "import_today", scale=self.import_export_scaling, required_unit="kWh")
                self.import_today_now = get_now_from_cumulative(self.import_today, self.minutes_now, backwards=True)
            else:
                self.log("Warn: You have not set import_today in apps.yaml, you will have no previous import data")

            # Load export today data
            if "export_today" in self.args:
                self.export_today = self.minute_data_import_export(self.max_days_previous, self.now_utc, "export_today", scale=self.import_export_scaling, required_unit="kWh")
                self.export_today_now = get_now_from_cumulative(self.export_today, self.minutes_now, backwards=True)
            else:
                self.log("Warn: You have not set export_today in apps.yaml, you will have no previous export data")

            # PV today data
            if "pv_today" in self.args:
                self.pv_today = self.minute_data_import_export(self.max_days_previous, self.now_utc, "pv_today", required_unit="kWh")
                self.pv_today_now = get_now_from_cumulative(self.pv_today, self.minutes_now, backwards=True)
            else:
                self.log("Warn: You have not set pv_today in apps.yaml, you will have no previous PV data")

        # Battery temperature
        if "battery_temperature_history" in self.args:
            self.battery_temperature_history = self.minute_data_import_export(self.max_days_previous, self.now_utc, "battery_temperature_history", scale=1.0, increment=False, smoothing=False)
            data = []
            for minute in range(0, 24 * 60, 5):
                data.append({minute: self.battery_temperature_history.get(minute, 0)})
            self.battery_temperature_prediction = self.predict_battery_temperature(self.battery_temperature_history, step=PREDICT_STEP)
            self.log("Fetched battery temperature history data, current temperature {}°C".format(self.battery_temperature_history.get(0, None)))

        # Car charging hold - when enabled battery is held during car charging in simulation
        self.car_charging_energy = self.load_car_energy(self.now_utc)

        # Log current values
        self.log("Current data so far today: load {}kWh, import {}kWh, export {}kWh, PV {}kWh".format(dp2(self.load_minutes_now), dp2(self.import_today_now), dp2(self.export_today_now), dp2(self.pv_today_now)))
        m = metrics()
        m.load_today_kwh.set(self.load_minutes_now)
        m.import_today_kwh.set(self.import_today_now)
        m.export_today_kwh.set(self.export_today_now)
        m.pv_today_kwh.set(self.pv_today_now)
        m.data_age_days.set(self.load_minutes_age)
        m.data_age_required_days.set(max(self.max_days_previous - 1, 0))

        if "rates_import_octopus_url" in self.args:
            # Fixed URL for rate import
            self.log("Downloading import rates directly from URL {}".format(self.get_arg("rates_import_octopus_url", indirect=False)))
            # Need to take a copy, as saving sessions will repeatedly increment cached import rates
            import_rates = copy.deepcopy(self.download_octopus_rates(self.get_arg("rates_import_octopus_url", indirect=False)))
        elif "metric_octopus_import" in self.args:
            # Octopus import rates
            entity_id = self.get_arg("metric_octopus_import", None, indirect=False)
            import_rates = self.fetch_octopus_rates(entity_id, adjust_key="is_intelligent_adjusted")
            if not import_rates:
                self.log("Error: metric_octopus_import is not set correctly in apps.yaml, or no energy rates can be read")
                self.record_status(message="Error: metric_octopus_import not set correctly in apps.yaml, or no energy rates can be read", had_errors=True)
        elif "metric_energidataservice_import" in self.args:
            # Energi Data Service import rates
            entity_id = self.get_arg("metric_energidataservice_import", None, indirect=False)
            import_rates = self.fetch_energidataservice_rates(entity_id, adjust_key="is_intelligent_adjusted")
            if not import_rates:
                self.log("Error: metric_energidataservice_import is not set correctly in apps.yaml, or no energy rates can be read")
                self.record_status(message="Error: metric_energidataservice_import not set correctly in apps.yaml, or no energy rates can be read", had_errors=True)
        elif "metric_stromligning_import_today" in self.args or "metric_stromligning_import_tomorrow" in self.args:
            # Strømligning import rates
            entity_id_today = self.get_arg("metric_stromligning_import_today", None, indirect=False)
            entity_id_tomorrow = self.get_arg("metric_stromligning_import_tomorrow", None, indirect=False)
            import_rates = self.fetch_stromligning_rates(entity_id_today, entity_id_tomorrow, adjust_key="is_intelligent_adjusted")
            if not import_rates:
                self.log("Error: metric_stromligning_import sensors are not set correctly or no energy rates can be read")
                self.record_status(message="Error: metric_stromligning_import sensors not set correctly or no energy rates can be read", had_errors=True)

        # Fallback if no other rate types are set
        if not import_rates:
            # Basic rates defined by user over time
            rate_import_dict = self.get_arg("rates_import", [], indirect=False)
            if rate_import_dict:
                import_rates = self.basic_rates(rate_import_dict, "rates_import")

        # Gas rates if set
        if "metric_octopus_gas" in self.args:
            entity_id = self.get_arg("metric_octopus_gas", None, indirect=False)
            self.rate_gas = self.fetch_octopus_rates(entity_id)
            if not self.rate_gas:
                self.log("Warn: metric_octopus_gas is not set correctly in apps.yaml, or no energy rates can be read")
                self.record_status(message="Warn: rate_import_gas not set correctly in apps.yaml, or no energy rates can be read", had_errors=True)
            else:
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

        # SoC history
        soc_kwh_data = self.get_history_wrapper(entity_id=self.prefix + ".soc_kw_h0", days=2, required=False)
        if soc_kwh_data:
            self.soc_kwh_history, _ = minute_data(
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
                can_modify_history=True,  # soc_kwh_data is not accessed after this point, so minute_data can freely modify it
            )

        # Fetch sensor data for cars, e.g. car plan, car energy, car sessions etc.
        self.dispatch_timeline_pending = []
        self.fetch_sensor_data_cars(save=save)
        # Dynamic load: cancel the Octopus Intelligent slots of a car that is in one but not charging -
        # before the rates are built, so a cancelled dispatch never gets its cheap rate
        dynamic_load_car_changed = self.dynamic_load_car_check(save=save)
        if save:
            # Remember the half hour a dispatch is running in, now it is known whether the car was cancelled
            self.octopus_held_update()
        octopus_surplus_changed = False

        if "rates_export_octopus_url" in self.args:
            # Fixed URL for rate export
            self.log("Downloading export rates directly from URL {}".format(self.get_arg("rates_export_octopus_url", indirect=False)))
            # Need to take a copy, as saving sessions will repeatedly increment cached export rates
            export_rates = copy.deepcopy(self.download_octopus_rates(self.get_arg("rates_export_octopus_url", indirect=False)))
        elif "metric_octopus_export" in self.args:
            # Octopus export rates
            entity_id = self.get_arg("metric_octopus_export", None, indirect=False)
            export_rates = self.fetch_octopus_rates(entity_id)
            if not export_rates:
                self.log("Warning: metric_octopus_export is not set correctly in apps.yaml, or no energy rates can be read")
                self.record_status(message="Error: metric_octopus_export not set correctly in apps.yaml, or no energy rates can be read", had_errors=True)
        elif "metric_energidataservice_export" in self.args:
            # Energi Data Service export rates
            entity_id = self.get_arg("metric_energidataservice_export", None, indirect=False)
            export_rates = self.fetch_energidataservice_rates(entity_id)
            if not export_rates:
                self.log("Warning: metric_energidataservice_export is not set correctly in apps.yaml, or no energy rates can be read")
                self.record_status(message="Error: metric_energidataservice_export not set correctly in apps.yaml, or no energy rates can be read", had_errors=True)
        elif "metric_stromligning_export_today" in self.args or "metric_stromligning_export_tomorrow" in self.args:
            # Strømligning export rates
            entity_id_today = self.get_arg("metric_stromligning_export_today", None, indirect=False)
            entity_id_tomorrow = self.get_arg("metric_stromligning_export_tomorrow", None, indirect=False)
            export_rates = self.fetch_stromligning_rates(entity_id_today, entity_id_tomorrow)
            if not export_rates:
                self.log("Warning: metric_stromligning_export sensors are not set correctly or no energy rates can be read")
                self.record_status(message="Error: metric_stromligning_export sensors not set correctly or no energy rates can be read", had_errors=True)

        # Fallback if no other rate types are set
        if not export_rates:
            # Basic rates defined by user over time
            rate_export_dict = self.get_arg("rates_export", [], indirect=False)
            # Allow all zero export rates, as some users have a feed-in tariff that is zero
            export_rates = self.basic_rates(rate_export_dict, "rates_export")

        # Fetch Axle sessions first so Octopus auto-join can skip saving sessions that overlap an Axle VPP session
        self.axle_sessions = fetch_axle_sessions(self)
        # Fetch octopus saving sessions and free sessions
        self.octopus_free_slots, self.octopus_saving_slots = self.fetch_octopus_sessions(self.axle_sessions)

        # Standing charge
        self.metric_standing_charge = self.get_arg("metric_standing_charge", 0.0) * 100.0
        self.log("Standing charge is set to {}{}".format(dp2(self.metric_standing_charge), curr))

        # futurerate data
        futurerate = FutureRate(self)
        self.future_energy_rates_import, self.future_energy_rates_export = futurerate.futurerate_analysis(import_rates, export_rates)

        # Replicate and scan import rates
        if import_rates:
            self.rate_scan(import_rates, print=False)
            self.rate_import_base, self.rate_min_base, self.rate_max_base = self.rate_base_min_max(import_rates)
            previous_surplus = self.octopus_surplus
            self.octopus_surplus = self.octopus_surplus_minutes()
            # The rates can move without the slots moving (the car's SoC changes how many half hours it needs)
            if save and self.octopus_surplus_changed(previous_surplus):
                self.log("Octopus Intelligent: dispatch minutes no car needs changed to {}".format(len(self.octopus_surplus)))
                octopus_surplus_changed = True
            import_rates = self.dynamic_load_car_strip_feed_rates(import_rates)
            import_rates, self.rate_import_replicated = self.rate_replicate(import_rates, self.io_adjusted, is_import=True, use_future=True)
            self.rate_import_no_io = import_rates.copy()
            for car_n in range(self.num_cars):
                import_rates = self.rate_add_io_slots(car_n, import_rates, self.octopus_slots[car_n])
            # Snapshot the tariff as it stands AFTER the IOG/SmartFlex dispatch overlay but BEFORE any
            # saving/free/Axle session distorts it (the overrides below are applied to it too).
            # rate_import_base is built further up
            # (ahead of rate_add_io_slots), so capping a saving minute against that would discard a
            # legitimate IOG discount wherever a session and a dispatch slot overlap, and the automatic
            # threshold scan could then miss a genuinely cheap slot.
            self.rate_import_pre_saving = import_rates.copy()
            self.load_saving_slot(self.octopus_saving_slots, import_rates, export=False, rate_replicate=self.rate_import_replicated)
            self.load_free_slot(self.octopus_free_slots, import_rates, export=False, rate_replicate=self.rate_import_replicated)
            load_axle_slot(self, self.axle_sessions, import_rates, export=False, rate_replicate=self.rate_import_replicated)
            # Snapshot which minutes a saving/free/Axle session tagged "saving" here, before
            # basic_rates()/apply_manual_rates() below get a chance to overwrite rate_replicate with
            # their own "increment"/"user" tag on the same minute - a supported combination (an
            # override active during a saving session). set_rate_thresholds() reads this frozen set,
            # not the live rate_import_replicated dict, so a later overwrite here can't erase the
            # "this was a saving minute" provenance it depends on (GH#5050).
            self.rate_import_saving_minutes = {minute for minute, tag in self.rate_import_replicated.items() if tag == "saving"}
            import_rates, self.rate_import_pre_saving = self.apply_rate_overrides(
                import_rates, self.rate_import_pre_saving, self.rate_import_saving_minutes, self.get_arg("rates_import_override", [], indirect=False), "rates_import_override", self.manual_import_rates, True, self.rate_import_replicated
            )
            self.rate_scan(import_rates, print=True)
        else:
            self.rate_import_no_io = {}
            self.log("Warning: No import rate data provided")
            self.record_status(message="Error: No import rate data provided", had_errors=True)
        # Atomic publish: readers (e.g. async components) never see a half-built or transiently-empty rate_import during rebuild.
        self.rate_import = import_rates
        # A charger's own schedule is costed at the tariff, which the car slots were built too early to know
        self.reprice_charger_schedule_slots()
        # Needs this cycle's dispatch decision and the final io_adjusted
        io_confirmed_changed = self.io_confirmed_credit_update(save=save)

        # Replicate and scan export rates
        if export_rates:
            self.rate_scan_export(export_rates, print=False)
            export_rates, self.rate_export_replicated = self.rate_replicate(export_rates, is_import=False, use_future=True)
            self.rate_export_base = export_rates.copy()
            # Built here, from the base rates, so the saving session and overrides applied below stay
            # out of it - battery_value_rate needs the tariff's own export price, not an event price
            self.rate_export_max_forward = self.rate_export_max_forward_calc(self.rate_export_base)
            # Export has no IOG overlay, so the pre-saving tariff matches rate_export_base - copied
            # rather than aliased so the two can never be corrupted together by a future in-place
            # mutation of either.
            self.rate_export_pre_saving = self.rate_export_base.copy()
            # For export tariff only load the saving session if enabled
            if self.rate_export_max > 0:
                self.load_saving_slot(self.octopus_saving_slots, export_rates, export=True, rate_replicate=self.rate_export_replicated)
            load_axle_slot(self, self.axle_sessions, export_rates, export=True, rate_replicate=self.rate_export_replicated)
            # See the import block's equivalent comment above (GH#5050).
            self.rate_export_saving_minutes = {minute for minute, tag in self.rate_export_replicated.items() if tag == "saving"}
            export_rates, self.rate_export_pre_saving = self.apply_rate_overrides(
                export_rates, self.rate_export_pre_saving, self.rate_export_saving_minutes, self.get_arg("rates_export_override", [], indirect=False), "rates_export_override", self.manual_export_rates, False, self.rate_export_replicated
            )
            self.rate_scan_export(export_rates, print=True)
        else:
            self.log("Warning: No export rate data provided")
            self.record_status(message="Error: No export rate data provided", had_errors=True)
        self.rate_export = export_rates
        # Not for the tariff comparison, which fetches with save off and would log every tariff it tries
        if save:
            self.log_replay_rates()

        # Set rate thresholds
        if self.rate_import or self.rate_export:
            self.set_rate_thresholds()

        # Needed here, ahead of "Find charging windows" below, because calc_pv_light_dark() reads
        # self.pv_forecast_minute to locate dawn - it was previously fetched further down in this
        # function, after the dawn calculation had already run against an empty dict, so the dawn
        # split could never actually trigger (#4699). Anything that reads it must still come after
        # this line: before it, the attribute holds last cycle's forecast (#4317).
        pv_light_dark = self.fetch_pv_forecast_and_dawn(save=save)

        # Find discharging windows
        if self.rate_export:
            self.high_export_rates, lowest, highest = self.rate_scan_window(self.rate_export, 5, self.rate_export_cost_threshold, True, alt_rates=self.rate_import)
            # Update threshold automatically
            self.log("High export rate found rates in range {}{} to {}{}".format(lowest, curr, highest, curr))
            if self.rate_high_threshold == 0 and lowest <= self.rate_export_max:
                self.rate_export_cost_threshold = lowest

        # Find charging windows
        if self.rate_import:
            self.find_low_rate_windows(alt_rates=self.rate_export, pv_light_dark=pv_light_dark)

        # #4516 Stage 1: render the dispatch timelines captured during the car fetch. This has to
        # come after the low-rate scan above, not merely after set_rate_thresholds(): in automatic
        # mode (rate_low_threshold 0) set_rate_thresholds() only sets a provisional
        # "everything but the most expensive" threshold, which the scan then replaces with the real
        # low-rate band. Rendering on the provisional value painted most of the day as cheap.
        self.log_dispatch_timelines()

        # Work out car plan?
        self.fetch_sensor_data_car_planning()
        if save:
            self.log_replay_cars()
        # Publish the car plan
        self.publish_car_plan()

        # Work out iBoost plan
        if self.iboost_enable and (((not self.iboost_solar) and (not self.iboost_charging)) or self.iboost_smart):
            self.iboost_plan = self.plan_iboost_smart()
            self.log(
                "iBoost iboost_solar {}, rate threshold import {}{}, rate threshold export {}{}, iboost_gas {}{}, iboost_gas_export {}{}, iboost_smart {}, min_length {}, plan is: {}".format(
                    self.iboost_solar,
                    self.iboost_rate_threshold,
                    curr,
                    self.iboost_rate_threshold_export,
                    curr,
                    self.iboost_gas,
                    curr,
                    self.iboost_gas_export,
                    curr,
                    self.iboost_smart,
                    self.iboost_smart_min_length,
                    self.iboost_plan,
                )
            )

        # Work out cost today
        if self.import_today:
            self.cost_today_sofar, self.carbon_today_sofar = self.today_cost(self.import_today, self.export_today, self.car_charging_energy, self.load_minutes, save=save)

        if self.load_minutes and not self.load_forecast_only and not self.load_forecast_history:
            # Apply modal filter to historical data. Skipped for days_previous_auto: the weighted-bucket
            # forecast deliberately ignores missing-data buckets, so the gap-filling/padding the modal filter
            # applies would mask exactly the gaps it is designed to exclude.
            self.previous_days_modal_filter(self.load_minutes)
            self.log("Historical days now {} weight {}".format(self.days_previous, self.days_previous_weight))

        # Weighted-bucket historical load forecast (days_previous_auto). Built here as it needs load_minutes
        # after the power fill. load_ml takes precedence if it already set the forecast-only load source.
        # Also runs with under a day of history, forecasting from what has been recorded today (batpred#5548).
        if self.load_forecast_history and self.load_minutes and not self.load_forecast_only:
            hist_forecast = self.compute_load_forecast_history(self.now_utc)
            if hist_forecast:
                self.load_forecast_only = True
                for minute, value in hist_forecast.items():
                    self.load_forecast[minute] = self.load_forecast.get(minute, 0) + value
                self.load_forecast_array.append(hist_forecast)
                self.log("Using weighted-bucket historical load forecast over {} days".format(min(self.load_minutes_age, self.max_days_previous - 1)))

        # Where Load ML genuinely supplied this cycle's forecast, backfill the elapsed part of today
        # with its own past predictions (the weighted-bucket forecast above is skipped in that case).
        # load_ml_forecast is this cycle's own fetch result from earlier in this function, so there is
        # no cross-cycle state that could leave a stale "ML was active" reading behind (#4762 review).
        self.apply_load_ml_forecast_history(self.now_utc, load_ml_forecast)

        if save:
            self.log_replay_load_forecast()

        # Load today vs actual
        if self.load_minutes:
            self.load_inday_adjustment = self.load_today_comparison(self.load_minutes, self.load_forecast, self.car_charging_energy, self.import_today, self.minutes_now, save=save)
        else:
            self.load_inday_adjustment = 1.0

        force_replan = dynamic_load_car_changed or octopus_surplus_changed or io_confirmed_changed
        # Compare on the change-detection signature, not the raw slots, so the per-cycle re-clocking
        # of an in-progress dispatch (start advanced to now, energy scaled to remaining time) does not
        # force a replan every cycle while a slot is active - only genuine slot changes do
        if self.octopus_slots_signature(prev_octopus_slots) != self.octopus_slots_signature(self.octopus_slots):
            self.log("Octopus slots changed from {} to {}".format(prev_octopus_slots, self.octopus_slots))
            force_replan = True
        if str(prev_octopus_saving_slots) != str(self.octopus_saving_slots):
            self.log("Octopus saving slots changed from {} to {}".format(prev_octopus_saving_slots, self.octopus_saving_slots))
            force_replan = True
        if str(prev_octopus_free_slots) != str(self.octopus_free_slots):
            self.log("Octopus free slots changed from {} to {}".format(prev_octopus_free_slots, self.octopus_free_slots))
            force_replan = True
        if str(prev_axle_sessions) != str(self.axle_sessions):
            self.log("Axle sessions changed from {} to {}".format(prev_axle_sessions, self.axle_sessions))
            force_replan = True
        return force_replan

    def fetch_sensor_data_car_planning(self):
        # Work out car plan?
        # Determine which cars are configured with Octopus Intelligent to avoid calling plan_car_charging for them
        iog_entity_id_config = self.get_arg("octopus_intelligent_slot", indirect=False)
        if iog_entity_id_config and not isinstance(iog_entity_id_config, list):
            iog_entity_ids = [iog_entity_id_config]
        elif iog_entity_id_config:
            iog_entity_ids = iog_entity_id_config
        else:
            iog_entity_ids = []

        for car_n in range(self.num_cars):
            if self.octopus_intelligent_charging and car_n < len(iog_entity_ids) and iog_entity_ids[car_n]:
                if self.car_charging_planned[car_n]:
                    self.log("Car {} on Octopus Intelligent, active plan for charge".format(car_n))
                else:
                    self.log("Car {} on Octopus Intelligent, no active plan".format(car_n))
            elif self.car_charging_planned[car_n] or self.car_charging_now[car_n]:
                limit_percent = dp1(self.car_charging_limit[car_n] / self.car_charging_battery_size[car_n] * 100) if self.car_charging_battery_size[car_n] else 0
                self.log(
                    "Car {} plan charging from {}kWh to {}% ({}kWh), with slots {}, ready by {}".format(
                        car_n,
                        self.car_charging_soc[car_n],
                        limit_percent,
                        self.car_charging_limit[car_n],
                        self.low_rates_tariff,
                        self.car_charging_plan_time[car_n],
                    )
                )
                # The tariff's own windows: a car cannot export, so the plan's pre-event windows are no use to it
                self.car_charging_slots[car_n] = self.plan_car_charging(car_n, self.low_rates_tariff)
            else:
                self.log("Car {} charging is not planned as it is not plugged in".format(car_n))

            # Log the charging plan
            if self.car_charging_slots[car_n]:
                self.log("Car {} charging plan is: {}".format(car_n, self.car_charging_slots[car_n]))

            # Only treat this car as "the" exclusive charging session if it actually produced a
            # real plan - in a shared-charger multi-car setup, car_charging_planned/now can read
            # true for every car profile sharing the same physical sensor (issue #4305), so a car
            # with an empty plan (its SoC already at/above its own limit) shouldn't block a later
            # car - the one actually plugged in and charging - from ever being considered.
            if (self.car_charging_planned[car_n] or self.car_charging_now[car_n]) and self.car_charging_exclusive[car_n] and self.car_charging_slots[car_n]:
                self.log("Car {} charging is exclusive, will not plan other cars".format(car_n))
                break

    def car_planned_slot_entity(self, planned_entity_id_list, car_n, slot_entity_id):
        """
        The sensor a car's planned dispatches are read from, where that is not its slot sensor.

        octopus_intelligent_slot supplies both the completed and the planned dispatches unless
        octopus_intelligent_planned_slot names another sensor for the car. None when it does not, or
        when it names the slot sensor itself - reading that twice would only repeat its dispatches.
        """
        if car_n >= len(planned_entity_id_list):
            return None
        planned_entity_id = planned_entity_id_list[car_n]
        if not planned_entity_id or not isinstance(planned_entity_id, str) or planned_entity_id.startswith("re:") or planned_entity_id == slot_entity_id:
            return None
        return planned_entity_id

    def car_arg_configured(self, arg, car_n, entity=False):
        """
        Whether apps.yaml sets arg for car car_n - with entity, only when it names an entity rather than a fixed value.

        A list gives each car its own entry, while a single value is taken as car 0's. An entry whose
        regular expression matched nothing is left as None, or as its unresolved re: pattern before
        the final auto_config() pass, and neither counts.
        """
        value = self.get_arg(arg, None, indirect=False)
        if isinstance(value, list):
            value = value[car_n] if car_n < len(value) else None
        elif car_n > 0:
            value = None
        if isinstance(value, str):
            if not value or value.startswith("re:"):
                return False
            return is_entity_id(value) if entity else True
        return value is not None and not entity

    def car_manual_soc_config(self):
        """
        Work out which cars have their SoC tracked manually, and which of those from a charger energy sensor.

        A car with no car_charging_soc sensor is tracked manually when what really happens can keep it right:
        a car_charging_energy sensor to count up the charge it takes, and a car_charging_plugged sensor to start
        it again from zero when it is unplugged. car_charging_planned is not enough, as many chargers turn it off
        when a charge finishes with the car still plugged in. Without both it is planned from empty every cycle, as before. Tracking it from the plan's predicted
        charge instead, with nothing to bring it back, would climb to the charge limit and stay there - and a
        car modelled as full has its Octopus Intelligent slots ignored and no battery hold during them.

        The car_charging_manual_soc switch forces manual tracking for any car.
        """
        self.car_charging_manual_soc = [False for c in range(max(self.num_cars, 1))]
        self.car_charging_manual_soc_energy = [False for c in range(max(self.num_cars, 1))]
        for car_n in range(self.num_cars):
            car_postfix = "" if car_n == 0 else "_" + str(car_n)
            switch = self.get_arg("car_charging_manual_soc" + car_postfix, False)
            energy = self.car_arg_configured("car_charging_energy", car_n, entity=True)
            automatic = energy and not self.car_arg_configured("car_charging_soc", car_n) and self.car_plug_arg(car_n) is not None
            self.car_charging_manual_soc[car_n] = bool(switch or automatic)
            self.car_charging_manual_soc_energy[car_n] = self.car_charging_manual_soc[car_n] and energy

    def car_manual_soc_energy_history(self, car_n, minutes):
        """
        The car's charger energy history covering at least the last minutes, as a cumulative kWh series
        indexed by minutes ago, or None when there is none.

        The car energy already loaded for the load forecast is reused when it is this car's one sensor;
        otherwise the car's own entry of car_charging_energy is fetched.
        """
        entities = self.get_arg("car_charging_energy", None, indirect=False)
        if not isinstance(entities, list):
            entities = [entities]
        if len(entities) == 1 and car_n == 0 and self.car_charging_energy:
            return self.car_charging_energy
        if car_n >= len(entities):
            return None
        days = min(self.max_days_previous, minutes // (24 * 60) + 1)
        return self.minute_data_import_export(days, self.now_utc, entities[car_n], scale=self.car_charging_energy_scale, required_unit="kWh") or None

    def car_manual_soc_energy_since(self, car_n, since, history):
        """
        kWh the car's charger energy sensor recorded between since and now, or None when it has no history.

        The history's cumulative series already counts on through the sensor resetting at midnight or at
        the start of a session. Before the history starts it reads zero, which is right - no energy was
        recorded then - but past its end it has no value, so a time older than that is taken as its start.

        history is the series car_manual_soc_energy_history() returned for a window covering since, so one
        read serves every window in a cycle.
        """
        minutes = int((self.now_utc - since).total_seconds() / 60)
        if minutes <= 0:
            return 0.0
        if not history:
            return None
        oldest = len(history) - 1 if isinstance(history, MinuteArray) else max(history.keys())
        return max(history.get(0, 0.0) - history.get(min(minutes, oldest), 0.0), 0.0)

    def car_manual_soc_energy_cap(self, car_n, since, energy_added):
        """
        Limit energy_added to what the car could have taken since then, at car_charging_rate with a 10% margin.

        A charger energy counter can glitch - drop to zero and come back, which the history counts as a new session -
        or jump. The battery size bounds the result anyway; this keeps one bad reading from filling the car.
        """
        rate = self.car_charging_rate[car_n] if car_n < len(self.car_charging_rate) else 0
        if not rate:
            return energy_added
        minutes = max((self.now_utc - since).total_seconds() / 60, 0)
        energy_max = rate * minutes / 60 * CAR_MANUAL_SOC_RATE_MARGIN + CAR_MANUAL_SOC_ENERGY_SLACK
        if energy_added > energy_max:
            self.log("Info: Car {} charger energy {} kWh since {} is more than it could take at {} kW, counting {} kWh".format(car_n, dp3(energy_added), since.strftime(TIME_FORMAT), rate, dp3(energy_max)))
            return energy_max
        return energy_added

    def car_manual_soc_kwh(self, car_n):
        """
        The manual SoC of car_n in kWh as the plan uses it: kept within zero and the car's battery size, so a value typed in
        for the wrong car - or in percent - cannot plan around a car that is fuller than it can be.
        """
        car_postfix = "" if car_n == 0 else "_" + str(car_n)
        try:
            soc = float(self.get_arg("car_charging_manual_soc_kwh" + car_postfix, 0.0))
        except (ValueError, TypeError):
            soc = 0.0
        battery_size = self.car_charging_battery_size[car_n] if car_n < len(self.car_charging_battery_size) else None
        if battery_size:
            soc = min(soc, battery_size)
        return dp3(max(soc, 0.0))

    def car_plug_arg(self, car_n, planned=False):
        """
        The apps.yaml setting whose sensor says whether the car is plugged in, or None when none names one.

        car_charging_plugged when it is set for the car to a sensor - a fixed value can never read unplugged. With planned,
        car_charging_planned otherwise: fine for telling that a car is not charging, but not for resetting its manual SoC,
        as many chargers (Ohme, some Wallbox states) turn it off when a charge finishes with the car still plugged in.
        """
        for arg in ("car_charging_plugged", "car_charging_planned") if planned else ("car_charging_plugged",):
            if self.car_arg_configured(arg, car_n, entity=True):
                return arg
        return None

    def car_manual_soc_time(self, value):
        """
        A time from a tracking record, or None when it is missing, unreadable or has no timezone.
        """
        try:
            when = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
        return when if when.tzinfo is not None else None

    def car_manual_soc_track(self):
        """
        Update the manual SoC of each manually tracked car for what has happened since the last cycle.

        Each car has a record in car_manual_soc_state: the value Predbat last saw or wrote, the time charger energy
        is counted from and how much of it since then has been added already, the plugged-in sensor's last reading and
        when the car was last active. It is published as
        sensor.predbat_car_manual_soc_tracked[_N] (see car_manual_soc_publish()), which a restart reads back, so
        tracking picks up where it left off. The entities' own change times are not used - Home Assistant only
        moves them when the state changes, while Predbat's own database moves them on every write.

        - A value that is not the one in the record was entered by the user. It is kept, and counting starts
          again from now, so no energy is added on top of it.
        - Otherwise the energy the car_charging_energy sensor recorded since the record's time is added, less
          charging losses - but no more than the car could have taken at car_charging_rate (car_manual_soc_energy_cap()).
          The history reaches Predbat up to a couple of minutes late and is smoothed back over those minutes when it does, so
          the time is not moved to now while energy is coming in: each cycle adds what the history now shows since that
          time less what is already counted. The cap therefore covers the whole charge so far until it bites. With nothing
          new the time moves up to CAR_MANUAL_SOC_LATE_MINUTES ago, if that is later, and the counted energy is worked out
          again for it from the same read. A read that is missing, or shows less than is already counted, leaves both
          alone. When the cap bites, counting starts again from now, so a counter glitch cannot keep feeding the window.
          The value kept is not limited to the battery size: that is not known until Octopus Intelligent has reported
          its own, later in the cycle, and the plan reads it clamped anyway (car_manual_soc_kwh()).
        - A car whose car_charging_plugged sensor has gone from plugged in to unplugged, and stayed unplugged for
          CAR_MANUAL_SOC_UNPLUG_MINUTES, goes back to zero - a charger reconnecting can read unplugged for a few minutes.
          Unavailable or unknown is no reading, so a sensor dropping out never resets the car, and a value entered while
          it is unplugged is kept.
        - A car with no car_charging_plugged sensor goes back to zero once it has been idle - no value entered, nothing
          charged or written by the plan, and car_charging_now not reading charging - for CAR_MANUAL_SOC_IDLE_MINUTES.

        A car with no record - new, or with no sensor to read back after a restart - starts counting from now.
        """
        for car_n in range(self.num_cars):
            if car_n >= len(self.car_charging_manual_soc) or not self.car_charging_manual_soc[car_n]:
                continue
            car_postfix = "" if car_n == 0 else "_" + str(car_n)
            name = "car_charging_manual_soc_kwh" + car_postfix
            soc = dp3(self.get_arg(name, 0.0))
            soc_new = soc
            plug_arg = self.car_plug_arg(car_n)
            plugged = self.car_plugged_in_now(car_n, plug_arg) if plug_arg else None

            record = self.car_manual_soc_state.get(car_n)
            if record is None:
                record = self.car_manual_soc_read(car_n)
            since = self.car_manual_soc_time(record.get("since")) if isinstance(record, dict) else None
            unplugged_since = None
            active = self.now_utc
            counted = 0.0
            if since is None:
                record = None

            if record is None:
                since = self.now_utc
            elif abs(soc - float(record.get("soc", soc))) >= 0.001:
                self.log("Car {} manual SoC was set to {} kWh, counting from now".format(car_n, soc))
                since = self.now_utc
            else:
                if not (car_n < len(self.car_charging_now) and self.car_charging_now[car_n]):
                    active = self.car_manual_soc_time(record.get("active")) or self.now_utc
                if car_n < len(self.car_charging_manual_soc_energy) and self.car_charging_manual_soc_energy[car_n]:
                    # The time stays put while energy comes in, and only what is new since the last cycle is added: history
                    # arrives late and is smoothed back over the minutes before it, so restarting from now each cycle lost that
                    counted = record.get("counted") or 0.0
                    # One read of the history serves the total and, with nothing new, the window the time moves up to
                    minutes = int((self.now_utc - since).total_seconds() / 60)
                    history = (self.car_manual_soc_energy_history(car_n, minutes) or {}) if minutes > 0 else {}
                    energy_total = self.car_manual_soc_energy_since(car_n, since, history)
                    capped = False
                    if energy_total:
                        energy_capped = self.car_manual_soc_energy_cap(car_n, since, energy_total)
                        capped = energy_capped < energy_total
                        energy_total = energy_capped
                    energy_added = (energy_total or 0.0) - counted
                    if energy_added >= 0.001:
                        soc_new = soc_new + energy_added * self.car_charging_loss
                        self.log("Car {} manual SoC {} kWh plus {} kWh charged is {} kWh ({} kWh since {})".format(car_n, dp3(soc), dp3(energy_added), dp3(soc_new), dp3(energy_total), since.strftime(TIME_FORMAT)))
                        counted = energy_total
                        active = self.now_utc
                        if capped:
                            # A counter glitch would stay in the window and keep the cap biting, so count on from now
                            since = self.now_utc
                            counted = 0.0
                    elif not counted or (energy_total is not None and energy_total >= counted - 0.001):
                        # Kept back a little, for energy the history records late - but not so far that every cycle reads days of it.
                        # A read that is missing or shows less than is already counted says nothing about it, so then both wait
                        late = self.now_utc - timedelta(minutes=CAR_MANUAL_SOC_LATE_MINUTES)
                        if late > since:
                            since = late
                            rebased = self.car_manual_soc_energy_since(car_n, since, history)
                            counted = self.car_manual_soc_energy_cap(car_n, since, rebased) if rebased else 0.0
                if plug_arg is None and soc_new > 0 and (self.now_utc - active).total_seconds() / 60 >= CAR_MANUAL_SOC_IDLE_MINUTES:
                    # Nothing says when the car is unplugged, so a car left idle this long is taken to have been driven since
                    self.log("Car {} has been idle since {}, resetting its manual SoC from {} kWh to 0".format(car_n, active.strftime(TIME_FORMAT), dp3(soc_new)))
                    soc_new = 0.0
                    since = self.now_utc
                    counted = 0.0
                    active = self.now_utc
                if record.get("plugged") is True and plugged is False:
                    # An unplug only counts once it has lasted: a charger reconnecting can read unplugged for a few minutes
                    unplugged_since = record.get("unplugged_since") or self.now_utc.isoformat()
                    try:
                        unplugged_minutes = (self.now_utc - datetime.fromisoformat(unplugged_since)).total_seconds() / 60
                    except (ValueError, TypeError):
                        unplugged_since, unplugged_minutes = self.now_utc.isoformat(), 0
                    if unplugged_minutes >= CAR_MANUAL_SOC_UNPLUG_MINUTES:
                        self.log("Car {} has been unplugged, resetting its manual SoC from {} kWh to 0".format(car_n, dp3(soc_new)))
                        soc_new = 0.0
                        since = self.now_utc
                        counted = 0.0
                        unplugged_since = None
                    else:
                        # Still plugged in, until the unplug has lasted
                        plugged = True
                elif plugged is None:
                    # No reading, so whatever was pending stays pending
                    unplugged_since = record.get("unplugged_since")

            soc_new = dp3(soc_new)
            if soc_new != soc:
                self.expose_config(name, soc_new)
            if plugged is None and record is not None:
                plugged = record.get("plugged")
            record_new = {"soc": soc_new, "since": since.isoformat(), "counted": dp3(counted), "plugged": plugged, "unplugged_since": unplugged_since, "active": active.isoformat()}
            # Published when the SoC, the counted energy or the plug changes. A time that moved on with the counted energy unchanged is
            # kept in memory, as after a restart the published, older pair gives the same SoC. The counted energy is published as it
            # changes, so a restart never pairs it with a time the history has since moved past. The active time moves with the SoC,
            # except while car_charging_now reads charging, where a restart only brings an idle reset forward
            publish = record is None or record.get("soc") != soc_new or record.get("counted") != record_new["counted"] or record.get("plugged") != plugged or record.get("unplugged_since") != unplugged_since
            self.car_manual_soc_state[car_n] = record_new
            if publish:
                self.car_manual_soc_publish(car_n)
            if car_n < len(self.car_charging_soc):
                self.car_charging_soc[car_n] = soc_new

    def car_manual_soc_written(self, car_n, soc):
        """
        Note in car_n's tracking record that Predbat itself has written soc to its manual SoC, so the next
        cycle does not take it for a value the user entered - and that the car was active, as the plan charged it.
        """
        record = self.car_manual_soc_state.get(car_n)
        if isinstance(record, dict) and record.get("soc") != soc:
            record["soc"] = soc
            record["active"] = self.now_utc.isoformat()
            self.car_manual_soc_publish(car_n)

    def car_manual_soc_entity(self, car_n):
        """
        The sensor car_n's tracking record is published as.
        """
        return "sensor." + self.prefix + "_car_manual_soc_tracked" + ("" if car_n == 0 else "_" + str(car_n))

    def car_manual_soc_publish(self, car_n):
        """
        Publish car_n's tracking record: the SoC as the state, with the time energy is counted from, the kWh already counted since then, the last plug reading,
        when a pending unplug was first read and when the car was last active as attributes.
        """
        record = self.car_manual_soc_state.get(car_n, {})
        self.dashboard_item(
            self.car_manual_soc_entity(car_n),
            state=record.get("soc"),
            attributes={
                "since": record.get("since"),
                "counted": record.get("counted"),
                "plugged": record.get("plugged"),
                "unplugged_since": record.get("unplugged_since"),
                "active": record.get("active"),
                "friendly_name": "Car {} manual SoC tracked".format(car_n),
                "unit_of_measurement": "kWh",
                "icon": "mdi:ev-station",
            },
        )

    def car_manual_soc_read(self, car_n):
        """
        Read car_n's tracking record back from its sensor, as after a restart, or None when there is none to read.
        """
        entity_id = self.car_manual_soc_entity(car_n)
        try:
            soc = float(self.get_state_wrapper(entity_id=entity_id, default=None))
        except (ValueError, TypeError):
            return None
        since = self.get_state_wrapper(entity_id=entity_id, attribute="since", default=None)
        counted = self.get_state_wrapper(entity_id=entity_id, attribute="counted", default=None)
        plugged = self.get_state_wrapper(entity_id=entity_id, attribute="plugged", default=None)
        unplugged_since = self.get_state_wrapper(entity_id=entity_id, attribute="unplugged_since", default=None)
        active = self.get_state_wrapper(entity_id=entity_id, attribute="active", default=None)
        if not isinstance(since, str):
            return None
        return {
            "soc": dp3(soc),
            "since": since,
            "counted": float(counted) if isinstance(counted, (int, float)) and not isinstance(counted, bool) and counted >= 0 else 0.0,
            "plugged": plugged if isinstance(plugged, bool) else None,
            "unplugged_since": unplugged_since if isinstance(unplugged_since, str) and unplugged_since else None,
            "active": active if isinstance(active, str) and active else None,
        }

    def car_manual_soc_mirror(self):
        """
        Show a car's sensor SoC in its manual SoC kWh entity when the car is not tracked manually.

        The entity is shown for every car, so this makes it plain that the plan is using the sensor and
        that changing the entity does nothing. Turning the switch on then starts from the car's real level.

        A car with no SoC sensor is left alone: there is no level to show, and writing the 0 it is planned from
        over a car that has only stopped being tracked for now - its switch turned off, or a regular expression
        that did not match at startup - would lose the tracked value for when it is tracked again.
        """
        for car_n in range(min(self.num_cars, len(self.car_charging_soc))):
            if car_n < len(self.car_charging_manual_soc) and self.car_charging_manual_soc[car_n]:
                continue
            if not self.car_arg_configured("car_charging_soc", car_n):
                continue
            car_postfix = "" if car_n == 0 else "_" + str(car_n)
            self.expose_config("car_charging_manual_soc_kwh" + car_postfix, dp3(self.car_charging_soc[car_n]))

    def fetch_sensor_data_cars(self, save=True):
        """
        Fetch car specific data such as Octopus intelligent slots and vehicle data if we can get it, and calculate current SoC and limits based on that

        `save` is False when compare.py re-runs the fetch for a candidate tariff rather than the
        live plan; diagnostics that record state across cycles must be skipped in that case.
        """

        # Work out current car SoC and limit
        self.car_charging_loss = 1 - float(self.get_arg("car_charging_loss"))

        # Move the manual car SoC on for what happened since the last cycle. Live plan only: it keeps
        # readings across cycles, which compare.py's re-runs would otherwise count a second time
        if save:
            self.car_manual_soc_track()

        # Initialise car_charging_soc BEFORE the IOG processing loop so that load_octopus_slots can access it.
        # Values will be recalculated per-car after car_charging_battery_size is updated from Octopus sensor data.
        self.car_charging_soc = [0.0 for car_n in range(self.num_cars)]
        self.car_charging_soc_next = [None for car_n in range(self.num_cars)]
        for car_n in range(self.num_cars):
            if car_n < len(self.car_charging_manual_soc) and self.car_charging_manual_soc[car_n]:
                self.car_charging_soc[car_n] = self.car_manual_soc_kwh(car_n)
            else:
                self.car_charging_soc[car_n] = (self.get_arg("car_charging_soc", 0.0, index=car_n) * self.car_charging_battery_size[car_n]) / 100.0

        # Get octopus intelligent slot configuration - could be single value or list for multiple cars
        entity_id_config = self.get_arg("octopus_intelligent_slot", indirect=False)

        # Normalize to list for multi-car support
        if entity_id_config and not isinstance(entity_id_config, list):
            entity_id_list = [entity_id_config]
        elif entity_id_config:
            entity_id_list = entity_id_config
        else:
            entity_id_list = []

        # A second sensor per car for the planned dispatches, where the one above only has a reliable
        # record of the completed ones - see car_planned_slot_entity()
        planned_entity_id_config = self.get_arg("octopus_intelligent_planned_slot", default=None, indirect=False)
        if planned_entity_id_config and not isinstance(planned_entity_id_config, list):
            planned_entity_id_list = [planned_entity_id_config]
        else:
            planned_entity_id_list = planned_entity_id_config or []

        # Cars whose charging plan came from Octopus Intelligent dispatch slots this cycle - used
        # below to decide which cars get a model-facing charge limit override (#4967)
        iog_slot_cars = []
        # The planned dispatches each car's plan used this cycle, for octopus_held_update()
        self.octopus_slots_planned = {}

        if entity_id_list:
            # Process each car's intelligent slot configuration
            for car_n in range(min(len(entity_id_list), self.num_cars)):
                entity_id = entity_id_list[car_n]
                if not entity_id:
                    continue

                completed = []
                planned = []
                started = []

                if entity_id and "octopus_intelligent_slot_action_config" in self.args:
                    config_entry = self.get_arg("octopus_intelligent_slot_action_config", None, indirect=False)
                    service_name = entity_id.replace(".", "/")
                    result = self.call_service_wrapper(service_name, config_entry=config_entry, return_response=True)
                    if result and ("slots" in result):
                        planned = result["slots"]
                    else:
                        self.log("Warn: Unable to get data from {} for car {} - octopus_intelligent_slot using action config {}, result was {}".format(entity_id, car_n, config_entry, result))
                else:
                    try:
                        completed = self.get_state_wrapper(entity_id=entity_id, attribute="completed_dispatches") or self.get_state_wrapper(entity_id=entity_id, attribute="completedDispatches")
                        planned = self.get_state_wrapper(entity_id=entity_id, attribute="planned_dispatches") or self.get_state_wrapper(entity_id=entity_id, attribute="plannedDispatches")
                        # Not merged into octopus_slots or used for any rate/plan decision yet - read
                        # only for the #4516 Stage 1 diagnostic timeline log below, to observe whether
                        # this is a trustworthy earlier-than-completed confirmation signal before
                        # building any gating logic on it (Stage 2, deferred).
                        started = self.get_state_wrapper(entity_id=entity_id, attribute="started_dispatches") or self.get_state_wrapper(entity_id=entity_id, attribute="startedDispatches")
                    except (ValueError, TypeError):
                        self.log("Warn: Unable to get data from {} for car {} - octopus_intelligent_slot may not be set correctly in apps.yaml".format(entity_id, car_n))
                        self.record_status(message="Error: octopus_intelligent_slot not set correctly in apps.yaml for car {}".format(car_n), had_errors=True)

                    planned_entity_id = self.car_planned_slot_entity(planned_entity_id_list, car_n, entity_id)
                    if planned_entity_id:
                        # The slot sensor above keeps the record of what was dispatched, and so billed
                        # off-peak (#5413); the plan of what is still to come is taken from here instead.
                        # What this sensor has already seen finish goes in behind the slot sensor's own
                        # completed list: it covers a slot from when it ends until the slot sensor lists
                        # it, and load_octopus_slots() drops whatever of it the slot sensor has by then
                        try:
                            planned = self.get_state_wrapper(entity_id=planned_entity_id, attribute="planned_dispatches") or self.get_state_wrapper(entity_id=planned_entity_id, attribute="plannedDispatches")
                            planned_completed = self.get_state_wrapper(entity_id=planned_entity_id, attribute="completed_dispatches") or self.get_state_wrapper(entity_id=planned_entity_id, attribute="completedDispatches")
                            completed = (completed or []) + (planned_completed or [])
                        except (ValueError, TypeError):
                            self.log("Warn: Unable to get data from {} for car {} - octopus_intelligent_planned_slot may not be set correctly in apps.yaml".format(planned_entity_id, car_n))
                            self.record_status(message="Error: octopus_intelligent_planned_slot not set correctly in apps.yaml for car {}".format(car_n), had_errors=True)

                # Completed and planned slots - merge from all cars
                if completed:
                    self.octopus_slots[car_n] += completed
                planned_used = []
                if planned and (not self.octopus_intelligent_ignore_unplugged or self.car_charging_planned[car_n] or self.car_charging_now[car_n]):
                    # We only count planned slots if the car is plugged in or we are ignoring unplugged cars. A car
                    # charging now is plugged in, even before car_charging_planned catches up with an ad-hoc dispatch
                    planned_used = planned
                # The half hours a planned dispatch has already run in, until the completed list has them (#5520).
                # After the completed list, which is the record once it arrives, and ahead of the planned one
                self.octopus_slots[car_n] += self.octopus_held_dispatches(car_n, completed or [], planned_used)
                self.octopus_slots[car_n] += planned_used
                self.octopus_slots_planned[car_n] = planned_used

                # Extract vehicle data if we can get it
                size = self.get_state_wrapper(entity_id=entity_id, attribute="vehicle_battery_size_in_kwh")
                rate = self.get_state_wrapper(entity_id=entity_id, attribute="charge_point_power_in_kw")
                try:
                    size = float(size)
                except (ValueError, TypeError):
                    size = None
                try:
                    rate = float(rate)
                except (ValueError, TypeError):
                    rate = None
                if size:
                    self.car_charging_battery_size[car_n] = size
                    # Recalculate car SoC now that battery size has been updated from Octopus data
                    if car_n < len(self.car_charging_manual_soc) and not self.car_charging_manual_soc[car_n]:
                        self.car_charging_soc[car_n] = (self.get_arg("car_charging_soc", 0.0, index=car_n) * self.car_charging_battery_size[car_n]) / 100.0
                    elif car_n < len(self.car_charging_manual_soc):
                        # Clamped again to Octopus's battery size, which can be larger than the configured one
                        self.car_charging_soc[car_n] = self.car_manual_soc_kwh(car_n)
                if rate:
                    # Octopus's figure only ever raises the configured rate, never lowers it. Where Octopus's catalogue
                    # gives the charger more than one power, the component passes on the lowest (catalogue_value(), #5438)
                    self.log("Car {} rate from Octopus is {}kW and configured rate {}".format(car_n, rate, self.car_charging_rate[car_n]))
                    self.car_charging_rate[car_n] = max(rate, self.car_charging_rate[car_n])
                    # ...but it must not raise it past the car's own maximum (car_charging_max_rate)
                    self.car_charging_rate[car_n] = self.car_rate_capped(car_n, self.car_charging_rate[car_n])

                # Get car charging limit again from car based on new battery size
                self.car_charging_limit[car_n] = dp3((float(self.get_arg("car_charging_limit", 100.0, index=car_n)) * self.car_charging_battery_size[car_n]) / 100.0)

                # #4516 Stage 1: diagnostic dispatch-timeline log. Purely observational - see
                # build_dispatch_timeline()'s and dispatch_timeline_should_log()'s docstrings.
                # Only the live plan's view is worth recording: compare.py re-runs this whole
                # fetch per candidate tariff (with save=False), which otherwise emits a burst of
                # near-identical timelines for plans that were never active - and, because each
                # one updates the "last logged" state, makes the real cycle's line look changed.
                #
                # Recorded here but rendered later: this runs before the rate fetch, so the cheap
                # ('-') background would read from the previous cycle's rates - and be empty
                # altogether on the first cycle after a restart. The dispatch lists and the
                # plugged-in reading are only correct here though: further down this function the
                # Octopus branch overwrites car_charging_planned with "has dispatch slots", so
                # capture the inputs now and render once the rates are known.
                if save:
                    self.dispatch_timeline_pending.append(
                        {
                            "car_n": car_n,
                            "completed": completed,
                            "started": started,
                            "planned": planned,
                            "plugged": self.car_charging_planned[car_n],
                            "charging_now": self.get_car_charging_now_tristate(car_n),
                        }
                    )

                # Extract vehicle preference if we can get it
                if self.octopus_intelligent_charging:
                    octopus_ready_time = self.get_arg("octopus_ready_time", None, index=car_n)
                    if isinstance(octopus_ready_time, str) and len(octopus_ready_time) == 5:
                        octopus_ready_time += ":00"
                    octopus_limit = self.get_arg("octopus_charge_limit", None, index=car_n)
                    if octopus_limit:
                        try:
                            octopus_limit = float(octopus_limit)
                        except (ValueError, TypeError):
                            self.log("Warn: octopus_charge_limit is set to a bad value {} for car {} in apps.yaml, must be a number".format(octopus_limit, car_n))
                            octopus_limit = None
                    if octopus_limit:
                        octopus_limit = dp3(float(octopus_limit) * self.car_charging_battery_size[car_n] / 100.0)
                        self.car_charging_limit[car_n] = min(self.car_charging_limit[car_n], octopus_limit)
                    if octopus_ready_time:
                        self.car_charging_plan_time[car_n] = octopus_ready_time

            # Get rate for import to compute charging costs
            if self.rate_import:
                self.rate_scan(self.rate_import, print=False)

            # Use octopus slots for charging - process for each car
            if self.octopus_intelligent_charging:
                for car_n in range(min(len(entity_id_list), self.num_cars)):
                    # car_charging_now adds no dispatch of its own: only Octopus's slots give the car a slot
                    # and the house its cheap rate
                    if not entity_id_list[car_n]:
                        continue
                    if not self.octopus_intelligent_ignore_unplugged or self.car_charging_planned[car_n] or self.car_charging_now[car_n]:
                        self.car_charging_slots[car_n] = self.load_octopus_slots(car_n, self.octopus_slots[car_n], self.octopus_intelligent_consider_full)
                        if self.car_charging_slots[car_n]:
                            iog_slot_cars.append(car_n)
                            self.log(
                                "Car {} using Octopus Intelligent, charging planned - charging limit {}, ready time {} - battery size {}".format(
                                    car_n, self.car_charging_limit[car_n], self.car_charging_plan_time[car_n], self.car_charging_battery_size[car_n]
                                )
                            )
                            self.car_charging_planned[car_n] = True
                        else:
                            self.log("Car {} using Octopus Intelligent, no charging is planned".format(car_n))
                            self.car_charging_planned[car_n] = False
                    else:
                        self.log("Car {} using Octopus Intelligent is unplugged".format(car_n))
                        self.car_charging_planned[car_n] = False
        else:
            # Disable octopus charging if we don't have the slot sensor
            self.octopus_intelligent_charging = False

        # Model-facing car charge limit (#4967). With octopus_intelligent_consider_full off (the
        # default) the prediction must trust the Octopus dispatch plan rather than modelling the car
        # filling up, so cars carrying IOG slots get an uncapped limit for predict()'s fill clamp
        # (which also releases the battery discharge hold once the modelled car "fills"). The real
        # car_charging_limit is left untouched - execute.py's "car is full" decision, the
        # plan_car_charging path and load_octopus_slots all still need it. None means no override.
        # A car reporting car_charging_now is drawing power whatever its modelled SoC says, so it is uncapped
        # too - execute_plan() holds the battery for it on the sensor alone, and a fill clamp zeroing its load
        # would leave the plan assuming the battery can discharge while execute holds it (#5245 review).
        uncapped_cars = set(iog_slot_cars) if not self.octopus_intelligent_consider_full else set()
        uncapped_cars.update(car_n for car_n in range(self.num_cars) if car_n < len(self.car_charging_now) and self.car_charging_now[car_n])
        if uncapped_cars:
            self.car_charging_limit_model = self.car_charging_limit[:]
            for car_n in uncapped_cars:
                self.car_charging_limit_model[car_n] = CAR_CHARGING_LIMIT_UNCAPPED
        else:
            self.car_charging_limit_model = None

        # Log final car SoC (initialised before the IOG loop, updated per-car after Octopus battery_size is read)
        if self.num_cars:
            car_charging_limit_percent = [dp1(limit / size * 100) if size else 0 for limit, size in zip(self.car_charging_limit, self.car_charging_battery_size)]
            self.log("Cars: SoC: {}kWh, Charge limit {}% ({}kWh), plan time {}, battery size {}kWh".format(self.car_charging_soc, car_charging_limit_percent, self.car_charging_limit, self.car_charging_plan_time, self.car_charging_battery_size))

        # Live plan only, as for car_manual_soc_track() - a compare.py re-run must not publish anything
        if save:
            self.car_manual_soc_mirror()

    def fetch_pv_forecast(self):
        """
        Fetch PV forecast data from one or more sensors

        Returns a tuple of (pv_forecast_minute, pv_forecast_minute10, pv_forecast_minute90) - the
        p50, p10 and p90 minute-indexed forecasts respectively. pv_forecast_minute90 is the real
        p90 series when the source published one (currently only Solcast), otherwise it is a plain
        copy of the p50 series - see the design spec for why the p90 upside is never synthesised
        from the p10 spread.
        """
        pv_forecast_minute = {}
        pv_forecast_minute10 = {}
        pv_forecast_minute90 = {}

        # Get data from forecast sensor
        entity_id = "sensor." + self.prefix + "_pv_forecast_raw"
        pv_forecast_packed_ld = self.get_state_wrapper(entity_id=entity_id, attribute="forecast")
        pv_forecast10_packed_ld = self.get_state_wrapper(entity_id=entity_id, attribute="forecast10")
        pv_forecast90_packed_ld = self.get_state_wrapper(entity_id=entity_id, attribute="forecast90")
        relative_time = self.get_state_wrapper(entity_id=entity_id, attribute="relative_time")
        try:
            relative_time = strptime_aware(relative_time, TIME_FORMAT)
        except (ValueError, TypeError):
            self.log(f"Warn: Unable to parse relative_time from PV forecast sensor: {relative_time}, using midnight UTC as fallback")
            relative_time = self.midnight_utc

        # Convert keys to integers and values to floats
        pv_forecast_packed = {}
        pv_forecast10_packed = {}
        pv_forecast90_packed = {}

        if pv_forecast_packed_ld:
            for key, value in pv_forecast_packed_ld.items():
                try:
                    minute = int(key)
                    pv_forecast_packed[minute] = float(value)
                except (ValueError, TypeError):
                    pass

        if pv_forecast10_packed_ld:
            for key, value in pv_forecast10_packed_ld.items():
                try:
                    minute = int(key)
                    pv_forecast10_packed[minute] = float(value)
                except (ValueError, TypeError):
                    pass

        if pv_forecast90_packed_ld:
            for key, value in pv_forecast90_packed_ld.items():
                try:
                    minute = int(key)
                    pv_forecast90_packed[minute] = float(value)
                except (ValueError, TypeError):
                    pass

        # Unpack the forecast data
        max_minute = max(pv_forecast_packed.keys()) if pv_forecast_packed else 0
        last_value = 0
        last_value10 = 0
        last_value90 = 0
        # The forecast could be for a different time to our relative time, so we need to offset the minutes to align with our midnight_utc.
        # relative_time is the midnight at which the forecast was saved, so stored minute keys are relative to that midnight.
        # We subtract the offset so that stored minute X (= relative_time + X) maps to (relative_time + X - midnight_utc) minutes from today's midnight.
        minute_offset = int((self.midnight_utc - relative_time).total_seconds() / 60)
        for minute in range(0, max_minute + 1):
            target_minute = minute - minute_offset
            last_value = pv_forecast_packed.get(minute, last_value)
            last_value10 = pv_forecast10_packed.get(minute, last_value10)
            last_value90 = pv_forecast90_packed.get(minute, last_value90)
            pv_forecast_minute[target_minute] = last_value
            pv_forecast_minute10[target_minute] = last_value10
            pv_forecast_minute90[target_minute] = last_value90

        # No p90 published (older sensor data, or a source that does not produce one) - fall back to
        # the central forecast. No upside is synthesised; see the design spec for why mirroring the
        # p10 spread was rejected.
        if not pv_forecast90_packed:
            pv_forecast_minute90 = dict(pv_forecast_minute)

        return pv_forecast_minute, pv_forecast_minute10, pv_forecast_minute90

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
                "device_class": "temperature",
                "icon": "mdi:temperature-celsius",
            },
        )
        return predicted_temp

    def download_ge_data(self, now_utc):
        """
        Gets the GE Cloud data into the required format
        """
        mdata = None
        if self.components:
            ge_cloud_data = self.components.get_component("gecloud_data")
            if ge_cloud_data:
                mdata, oldest_data_time = ge_cloud_data.get_data()

        if not mdata:
            self.log("Warn: No GE Cloud data returned from GECloudData component, check if it is configured correctly")
            return

        age = now_utc - oldest_data_time
        self.load_minutes_age = age.days
        self.load_minutes, _ = minute_data(ge_cloud_data.filter_data(mdata, "consumption"), self.max_days_previous, now_utc, "consumption", "last_updated", backwards=True, smoothing=True, scale=1.0, clean_increment=True, interpolate=True)
        self.import_today, _ = minute_data(ge_cloud_data.filter_data(mdata, "import"), self.max_days_previous, now_utc, "import", "last_updated", backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True)
        self.export_today, _ = minute_data(ge_cloud_data.filter_data(mdata, "export"), self.max_days_previous, now_utc, "export", "last_updated", backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True)
        self.pv_today, _ = minute_data(ge_cloud_data.filter_data(mdata, "pv"), self.max_days_previous, now_utc, "pv", "last_updated", backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True)

        self.load_minutes_now = get_now_from_cumulative(self.load_minutes, self.minutes_now, backwards=True)
        self.load_minutes_source = "ge_cloud"
        self.load_last_period = (self.load_minutes.get(0, 0) - self.load_minutes.get(PREDICT_STEP, 0)) * 60 / PREDICT_STEP
        self.import_today_now = get_now_from_cumulative(self.import_today, self.minutes_now, backwards=True)
        self.export_today_now = get_now_from_cumulative(self.export_today, self.minutes_now, backwards=True)
        self.pv_today_now = get_now_from_cumulative(self.pv_today, self.minutes_now, backwards=True)

        # More up to date sensors for current values if set
        if "load_today" in self.args:
            load_minutes, load_minutes_age = self.minute_data_load(self.now_utc, "load_today", self.max_days_previous, required_unit="kWh", load_scaling=1.0, interpolate=True)
            self.load_minutes_now = get_now_from_cumulative(load_minutes, self.minutes_now, backwards=True)
            self.load_last_period = (load_minutes.get(0, 0) - load_minutes.get(PREDICT_STEP, 0)) * 60 / PREDICT_STEP
            self.log("GECloudData load_last_period from immediate sensor is {} kW".format(dp2(self.load_last_period)))

        if "import_today" in self.args:
            import_today = self.minute_data_import_export(self.max_days_previous, self.now_utc, "import_today", scale=self.import_export_scaling, required_unit="kWh")
            self.import_today_now = get_now_from_cumulative(import_today, self.minutes_now, backwards=True)

        # Load export today data
        if "export_today" in self.args:
            export_today = self.minute_data_import_export(self.max_days_previous, self.now_utc, "export_today", scale=self.import_export_scaling, required_unit="kWh")
            self.export_today_now = get_now_from_cumulative(export_today, self.minutes_now, backwards=True)

        # PV today data
        if "pv_today" in self.args:
            pv_today = self.minute_data_import_export(self.max_days_previous, self.now_utc, "pv_today", required_unit="kWh")
            self.pv_today_now = get_now_from_cumulative(pv_today, self.minutes_now, backwards=True)

        self.log("Downloaded {} datapoints from GECloudData going back {} days".format(len(self.load_minutes), self.load_minutes_age))
        return True

    def rate_replicate(self, rates, rate_io=None, is_import=True, is_gas=False, use_future=False):
        """
        We don't get enough hours of data for Octopus, so lets assume it repeats until told others

        use_future fills missing minutes from the predicted future rates (futurerate). Those are
        predictions for the live import/export tariff only, so other rates replicated through here
        (gas, and the tariffs compare and annual replay) must leave it off.
        """
        if rate_io is None:
            rate_io = {}
        minute = -24 * 60
        rate_last = 0
        rate_last_valid = False  # Track if we've seen any real rates yet
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

                if rate_io and (minute_mod in rate_io) and rate_io[minute_mod]:
                    # Dont replicate Intelligent rates into the next day as it will be different
                    rate_offset = self.rate_max
                    using_last = False
                elif minute_mod in rates:
                    rate_offset = rates[minute_mod]
                    using_last = False
                else:
                    # Missing rate within 24 hours - fill with dummy last rate
                    rate_offset = rate_last
                    using_last = True

                futurerate_adjust_import = self.get_arg("futurerate_adjust_import", False)
                futurerate_adjust_export = self.get_arg("futurerate_adjust_export", False)
                # A predicted future rate is an absolute price for this minute, so use it whenever there
                # is one - even where the minute being copied was itself filled, or has no prediction
                if use_future and is_import and futurerate_adjust_import and (minute in self.future_energy_rates_import):
                    rate_offset = self.future_energy_rates_import[minute]
                    adjust_type = "future"
                    using_last = False
                    adjusted_rates[minute] = True
                elif use_future and (not is_import) and (not is_gas) and futurerate_adjust_export and (minute in self.future_energy_rates_export):
                    rate_offset = max(self.future_energy_rates_export[minute], 0)
                    adjust_type = "future"
                    using_last = False
                    adjusted_rates[minute] = True
                # Only offset once not every day
                elif minute_mod not in adjusted_rates:
                    if is_import:
                        rate_offset = rate_offset + self.metric_future_rate_offset_import
                        if self.metric_future_rate_offset_import:
                            adjust_type = "offset"
                    elif (not is_import) and (not is_gas):
                        rate_offset = max(rate_offset + self.metric_future_rate_offset_export, 0)
                        if self.metric_future_rate_offset_export:
                            adjust_type = "offset"

                    adjusted_rates[minute] = True

                # Don't add rates if we haven't seen any real rates yet
                # This prevents filling negative minutes with invalid fallback values
                if rate_last_valid or not using_last:
                    rates[minute] = rate_offset
                    replicated_rates[minute] = adjust_type
            else:
                rate_last = rates[minute]
                rate_last_valid = True
            minute += 1

        return rates, replicated_rates

    def carbon_replicate(self, carbon_data):
        """
        Extend the carbon intensity forecast forward to cover the whole plan horizon.

        The Carbon Intensity API only publishes about 48 hours ahead, and publishes a good deal
        less than that whenever the upstream forecast is late, so the tail of the plan can easily
        have no data at all. A missing minute is scored as zero gCO2/kWh by prediction.py, which
        makes the uncovered part of the plan look carbon free and biases the optimiser towards it,
        so fill the gaps the same way rate_replicate does for missing rates - repeat the same time
        of day 24 hours earlier, falling back to the last known value where there is no such point
        yet. Carbon intensity has a strong daily cycle (the solar dip and the evening peak), so the
        previous day is a far better estimate than either zero or a flat average.

        Nothing is invented before the first real value, so an empty forecast stays empty rather
        than becoming a plan full of fabricated zeroes.

        :param carbon_data: carbon intensity in gCO2/kWh by minute relative to now
        :return: the extended data and a dict of which minutes were replicated
        """
        replicated_carbon = {}
        carbon_last = None
        minute = 0

        while minute < self.forecast_minutes:
            if minute in carbon_data:
                carbon_last = carbon_data[minute]
            elif carbon_last is not None:
                # Take the same time of day yesterday, which may itself have been replicated
                # so that the daily cycle keeps repeating across the whole horizon
                previous_day = minute - 24 * 60
                carbon_data[minute] = carbon_data[previous_day] if previous_day in carbon_data else carbon_last
                replicated_carbon[minute] = True
            minute += 1

        return carbon_data, replicated_carbon

    def calc_pv_light_dark(self):
        """
        Compute the dawn light/dark split via calc_dawn().

        Always computed, even when combine_charge_slots is off: with it off, find_charge_window
        already forces a window break every charge_slot_split minutes (which equals
        plan_interval_minutes, the same granularity calc_dawn buckets at), so the dawn boundary can
        never actually be reached there and calc_dawn's result is a no-op for window splitting in
        that case - but binary_sensor.predbat_dawn (published from this same result, see
        fetch_pv_forecast_and_dawn) is a useful standalone signal regardless of combine_charge_slots,
        and used to read permanently "off" for anyone with it disabled (the default) even in broad
        daylight, which is what it is not meant to mean.
        """
        return self.calc_dawn()

    def calc_dawn(self):
        """
        Find dawn in self.pv_forecast_minute and return a pv_light_dark dict classifying each minute
        as light (1, at/after dawn) or dark (0, before it). Used by find_charge_window (via
        calc_pv_light_dark) to split a charge window at the light/dark boundary - originally so it
        wouldn't abandon low power charging for a whole window just because its tail overlaps the sun
        (#4557), and now also so the plan optimizer can choose the dark portion of a combined window
        independently of the light portion.

        Classified per plan_interval_minutes bucket (averaged), not per raw minute - a threshold
        compared minute to minute would let ordinary forecast noise near the cutoff (e.g. a patchy dawn
        hovering around the threshold) retrigger the split repeatedly and chop the window into several
        small pieces. Bucketing makes the classification a step function that can change at most once
        per bucket boundary, at the same granularity the plan already displays.

        Within each calendar day, light is a one-way latch: the first bucket that crosses the threshold
        confirms dawn, and every later bucket that day stays light too, even if PV genuinely dips back
        under the threshold later (a cloud passing). This is a dawn detector, not a tracker of every
        rise and fall - it only cares about finding the first dawn each day, so an intermittently
        cloudy morning can't chop the window into several flip-flopping pieces. The latch resets at
        each day boundary so the next day's dawn is found independently rather than one early crossing
        holding "light" for the rest of the multi-day forecast horizon - this also makes a polar-night
        day correctly stay all-dark (dawn never crosses) and a polar-day day correctly stay all-light
        (crossed from the very first bucket).

        The threshold is the user-configurable low_power_pv_threshold_w, an absolute Watts figure,
        rather than a fraction of this forecast's own peak. A fraction-of-peak threshold sounds like it
        "scales with the site", but it actually scales with today's weather: a heavily overcast day has
        a low peak of its own, so a fixed fraction of it can call a trickle of PV "light" at a much
        lower absolute wattage than the same fraction would on a clear day - and there is nothing in
        Predbat today recording the site's real panel capacity to normalise against instead (only
        inverter_limit, which caps the inverter's own output, not the array's rating). An absolute
        figure the user sets once for their own system does not have that problem (#4699 follow-up).
        The same value and the same comparison is reused by find_charge_rate's low-power abandon check
        (utils.py) - one threshold answering "is this bright enough to matter" everywhere it is asked,
        rather than two independently-tuned ones that could disagree on genuine twilight PV.

        Built from self.pv_forecast_minute as populated earlier this same fetch_sensor_data() call by
        fetch_pv_forecast() - see #4699, where calc_pv_light_dark() ran before that populating call and
        so always saw the freshly-reset empty dict, silently disabling the dawn split for everyone.

        Logs the calculated dawn time (today's, or the earliest day the forecast reaches if today's PV
        data isn't there) each time it runs, so it's visible whether the detected dawn looks sane.
        """
        pv_light_dark = {}
        if not self.pv_forecast_minute:
            return pv_light_dark

        light_threshold = self.low_power_pv_threshold_w / MINUTE_WATT
        interval = self.plan_interval_minutes
        bucket_sums = {}
        bucket_counts = {}
        for pv_minute, pv in self.pv_forecast_minute.items():
            bucket = pv_minute // interval
            bucket_sums[bucket] = bucket_sums.get(bucket, 0.0) + pv
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        bucket_crossed = {bucket: (1 if (bucket_sums[bucket] / bucket_counts[bucket]) >= light_threshold else 0) for bucket in bucket_sums}

        bucket_light = {}
        dawn_minute_by_day = {}
        after_dawn = False
        current_day = None
        for bucket in sorted(bucket_crossed):
            bucket_day = (bucket * interval) // (24 * 60)
            if bucket_day != current_day:
                after_dawn = False
                current_day = bucket_day
            if bucket_crossed[bucket]:
                if not after_dawn:
                    dawn_minute_by_day[bucket_day] = bucket * interval
                after_dawn = True
            bucket_light[bucket] = 1 if after_dawn else 0

        # Day 0 is today (self.minutes_now is itself minutes since midnight_utc), so report today's
        # dawn when the forecast reaches it; otherwise fall back to the earliest day that does (e.g. a
        # forecast that only starts covering PV from tomorrow) so the log still says something useful.
        report_day = 0 if 0 in dawn_minute_by_day else (min(dawn_minute_by_day) if dawn_minute_by_day else None)
        if report_day is not None:
            dawn_timestamp = self.midnight_utc + timedelta(minutes=dawn_minute_by_day[report_day])
            self.log("Calculated dawn (start of daylight, used to split charge windows at) at {}".format(dawn_timestamp.strftime(TIME_FORMAT)))
        else:
            self.log("Calculated dawn (start of daylight, used to split charge windows at) - no dawn found in the PV forecast")

        pv_light_dark = {pv_minute: bucket_light[pv_minute // interval] for pv_minute in self.pv_forecast_minute}
        return pv_light_dark

    def find_charge_window(self, rates, minute, threshold_rate, find_high, alt_rates=None, pv_light_dark=None):
        """
        Find the charging windows based on the low rate threshold (percent below average)

        pv_light_dark, when scanning for charge (not find_high) windows, is a minute-indexed dict of 0/1
        marking whether PV forecast is at/after dawn ("light") or not ("dark") - see calc_dawn and
        calc_pv_light_dark. A dark -> light transition (dawn) forces a window split, so a charge window
        that would otherwise span sunrise (e.g. a single long cheap-rate period) is instead built as
        separate dark and light windows. Originally added (#4557) so low power charging wasn't
        defeated for the whole window, including the still-dark hours, just because the window's tail
        overlapped PV later on - it also lets the plan optimizer pick the dark portion of a combined
        window without the light portion, regardless of low power charging.
        """
        alt_rates = alt_rates or {}
        pv_light_dark = pv_light_dark or {}
        rate_low_start = -1
        rate_low_end = -1
        rate_low_average = 0
        rate_low_rate = 0
        rate_low_count = 0
        alternate_rate_boundary = False
        alt_rate_last = None
        pv_boundary = False
        pv_light_dark_last = None

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
            pv_state = pv_light_dark.get(minute, None)
            if (rate_low_start >= 0) and (pv_state == 1) and (pv_light_dark_last == 0):
                # Create a PV boundary at dawn (dark -> light) since the window started. Gated on
                # rate_low_start so a transition crossed only during the pre-window search phase
                # doesn't leave a stale boundary that breaks the window the moment it starts.
                # Light -> dark is not split on: calc_dawn latches each day light from dawn until
                # midnight, so that step falls at 00:00 in the dark and would cut an overnight
                # cheap window (e.g. 23:30-05:30) in two despite combine_charge_slots.
                pv_boundary = True
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
                    if (rate_low_start in self.manual_all_times or minute in self.manual_all_times) and (rate_low_start >= 0) and ((minute - rate_low_start) >= self.plan_interval_minutes):
                        # Manual slot
                        rate_low_end = minute
                        break
                    if find_high and (rate_low_start >= 0) and (((minute - rate_low_start) >= 60 * 24) or (((minute - rate_low_start) >= self.plan_interval_minutes) and alternate_rate_boundary)):
                        # Export slot can never be bigger than 4 hours
                        rate_low_end = minute
                        break
                    if (not find_high) and (rate_low_start >= 0) and ((minute - rate_low_start) >= self.plan_interval_minutes) and pv_boundary:
                        # Split a charge window where PV forecast crosses the light/dark threshold
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
            pv_light_dark_last = pv_state

        if rate_low_count:
            rate_low_average = dp2(rate_low_average / rate_low_count)
        return rate_low_start, rate_low_end, rate_low_average

    def pre_saving_for_session_minutes(self, pre_saving, saving_minutes):
        """
        Keep only the session minutes of a pre-saving snapshot, the only ones rate_minmax_excluding_saving() reads

        The full table is needed while the overrides are applied to it, but kept whole it would put two
        extra per-minute rate tables in every debug dump, on days without a session too.
        """
        return {minute: pre_saving[minute] for minute in saving_minutes if minute in pre_saving}

    def apply_rate_overrides(self, rates, pre_saving, saving_minutes, override_items, rtype, manual_items, is_import, rate_replicate):
        """
        Apply the configured rate overrides and then the manual rates on top of rates, tagging each
        overridden minute in rate_replicate. Returns (rates, pre_saving).

        When there are saving minutes the same overrides also go on pre_saving - the rates without
        any saving/free/Axle session - so it stays "these rates without the session". Otherwise
        rate_minmax_excluding_saving() would cap an overridden session minute (a fixed 50p to
        discourage charging in the session, say) back to the bare tariff and the override would
        drop out of the threshold stats. With no saving minutes nothing reads pre_saving, and with no
        overrides there is nothing to apply, so either way it is left alone. Only the session minutes of
        pre_saving are returned (see pre_saving_for_session_minutes()).
        """
        rates_before = rates
        rates = self.basic_rates(override_items, rtype, rates, rate_replicate)
        rates = self.apply_manual_rates(rates, manual_items, is_import=is_import, rate_replicate=rate_replicate)
        if saving_minutes and pre_saving and (override_items or manual_items):
            pre_saving = self.override_session_rates(rates_before, pre_saving, saving_minutes, override_items, rtype, manual_items, is_import)
        return rates, self.pre_saving_for_session_minutes(pre_saving, saving_minutes)

    def override_session_rates(self, rates, pre_saving, saving_minutes, override_items, rtype, manual_items, is_import, include_manual_api=True):
        """
        Apply overrides (and manual rates) to the session minutes' pre-session rates, returning them for the session minutes only

        basic_rates() works over a whole rate table - it wraps and bounds override ranges by its last
        minute - so the pre-session rates are laid over `rates` (the table before these overrides) to give
        it one, and the result is cut back to the session minutes.
        """
        table = dict(rates)
        table.update(pre_saving)
        table = self.basic_rates(override_items, rtype, table, {}, include_manual_api=include_manual_api)
        table = self.apply_manual_rates(table, manual_items, is_import=is_import, rate_replicate={})
        # Only minutes that had a pre-session rate: one a session created has no tariff rate to be capped to
        return {minute: table[minute] for minute in saving_minutes if minute in pre_saving and minute in table}

    def apply_manual_rates(self, rates, manual_items, is_import=True, rate_replicate=None):
        """
        Apply manual rates to the rates dictionary
        """
        if rate_replicate is None:
            rate_replicate = {}
        if not manual_items:
            return rates

        for minute in manual_items:
            rate = manual_items[minute]
            try:
                rate = float(rate)
            except (ValueError, TypeError):
                self.log("Warn: Bad rate {} provided in manual rates".format(rate))
                self.record_status("Warn: Bad rate {} provided in manual rates".format(rate), had_errors=True)
                continue
            rates[minute] = rate
            rate_replicate[minute] = "manual"

        return rates

    def basic_rates(self, info, rtype, prev=None, rate_replicate=None, include_manual_api=True):
        """
        Work out the energy rates based on user supplied time periods. Weekly rules on the base
        tariff are stamped across the whole horizon, each day against its own day_of_week; any
        minute no rule claims is left for rate_replicate() to fill later

        include_manual_api should be False for callers (e.g. tariff comparison, annual replay)
        that simulate a tariff other than the live one - the live system's manual API overrides
        apply to the actual running plan and must not leak into those simulations.
        """
        rates = {}
        if rate_replicate is None:
            rate_replicate = {}
        curr = self.currency_symbols[1]

        if prev:
            rates = prev.copy()
            max_minute = max(rates) + 1
        else:
            # Set to zero
            for minute in range(24 * 60):
                rates[minute] = 0
            max_minute = 48 * 60

        # get_manual_api() returns each override wrapped as {"index": ..., "value": {...}}, not the
        # flat {"start": ..., "end": ..., "rate": ...} shape this loop expects below - unwrap it here.
        # Before this fix, an unwrapped entry's this_rate.get("start")/get("rate") always missed,
        # falling through to the "00:00:00" start/end default (which, since end<=start, wraps to a
        # full 24-hour range) and the rate=0/rate_increment=True default (a genuine no-op on the
        # rate value) - silently marking every minute of the day as overridden in rate_replicate
        # (and hence in the plan display) even for the narrowest override window (batpred#2578).
        # For most callers (anything sourced via get_arg(..., default=[]), which already merges
        # manual API overrides into its own return value) this would duplicate an override already
        # present in `info`. Avoid double-applying, as incremental overrides would otherwise stack.
        manual_items = []
        if include_manual_api:
            for item in self.get_manual_api(rtype):
                value = item.get("value")
                if not isinstance(value, dict):
                    self.log("Warn: Manual API override for {} must use the '?start=...&end=...&rate=...' form, got {}".format(rtype, value))
                    self.record_status("Warn: Manual API override for {} must use the '?start=...&end=...&rate=...' form".format(rtype), had_errors=True)
                    continue
                if value not in info:
                    manual_items.append(value)
        if manual_items:
            self.log("Basic rate API override items for {} are {}".format(rtype, manual_items))

        midnight = time_string_to_stamp("00:00:00")
        for this_rate in info + manual_items:
            if this_rate and isinstance(this_rate, dict):
                start_str = this_rate.get("start") or "00:00:00"
                start_str = self.resolve_arg("start", start_str, "00:00:00") or "00:00:00"
                end_str = this_rate.get("end") or "00:00:00"
                end_str = self.resolve_arg("end", end_str, "00:00:00") or "00:00:00"
                load_scaling = this_rate.get("load_scaling", None)

                if start_str.count(":") < 2:
                    start_str += ":00"
                if end_str.count(":") < 2:
                    end_str += ":00"

                start = time_string_to_stamp(start_str)
                if start is None:
                    self.log("Warn: Bad start time {} provided in energy rates".format(start_str))
                    self.record_status("Warn: Bad start time {} provided in energy rates".format(start_str), had_errors=True)
                    continue

                end = time_string_to_stamp(end_str)
                if end is None:
                    self.log("Warn: Bad end time {} provided in energy rates".format(end_str))
                    self.record_status("Warn: Bad end time {} provided in energy rates".format(end_str), had_errors=True)
                    continue

                date = None
                if "date" in this_rate:
                    date_str = self.resolve_arg("date", this_rate["date"])
                    try:
                        date = datetime.strptime(date_str, "%Y-%m-%d")
                    except (ValueError, TypeError):
                        self.log("Warn: Bad date {} provided in energy rates".format(this_rate["date"]))
                        self.record_status("Warn: Bad date {} provided in energy rates".format(this_rate["date"]), had_errors=True)
                        continue
                day_of_week = []
                if "day_of_week" in this_rate:
                    day = str(this_rate["day_of_week"])
                    days = day.split(",")
                    for day in days:
                        try:
                            day = int(day)
                        except (ValueError, TypeError):
                            self.log("Warn: Bad day_of_week {} provided in energy rates, should be 1-7".format(day_of_week))
                            self.record_status("Warn: Bad day_of_week {} provided in energy rates, should be 1-7".format(day_of_week), had_errors=True)
                            continue
                        if day < 1 or day > 7:
                            self.log("Warn: Bad day_of_week {} provided in energy rates, should be 1-7".format(day))
                            self.record_status("Warn: Bad day_of_week {} provided in energy rates, should be 1-7".format(day), had_errors=True)
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
                if isinstance(rate, str) and rate and rate[0].isalpha():
                    rate = self.resolve_arg("rate", rate, 0.0)
                if isinstance(load_scaling, str) and load_scaling and load_scaling[0].isalpha():
                    load_scaling = self.resolve_arg("load_scaling", load_scaling, 1.0)

                # Ensure the end result is a float
                try:
                    rate = float(rate)
                except (ValueError, TypeError):
                    self.log("Warn: Bad rate {} provided in energy rates".format(rate))
                    self.record_status("Warn:Bad rate {} provided in energy rates".format(rate), had_errors=True)
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

                # A window flagged utc is given in UTC rather than local wall-clock time, so shift it
                # by the local offset. That keeps a UTC-fixed schedule - an Economy 7 smart meter, for
                # instance - aligned with the meter through British Summer Time instead of running an
                # hour early. The offset is taken at local midnight, which is the reference the
                # returned minute keys are relative to.
                if this_rate.get("utc", False):
                    utc_offset = self.midnight_utc.utcoffset()
                    offset_minutes = int(utc_offset.total_seconds() // 60) if utc_offset else 0
                    start_minutes += offset_minutes
                    end_minutes += offset_minutes

                # Adjust for date if specified
                if date:
                    # Carry midnight_utc's offset so this stays a plain wall-clock difference in
                    # whole days, whichever side of a DST change the date falls on.
                    delta_minutes = minutes_to_time(date.replace(tzinfo=self.midnight_utc.tzinfo), self.midnight_utc)
                    start_minutes += delta_minutes
                    end_minutes += delta_minutes

                self.log(
                    "Adding rate {}: {}{} => {} to {} @ {}{}, date {}, day_of_week {}, increment {}{}".format(rtype, this_rate, curr, self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), rate, curr, date, day_of_week, rate_increment, curr)
                )

                day_of_week_midnight = self.midnight_utc.weekday()

                # A weekly rule on the base tariff is stamped over the whole horizon rate_replicate()
                # goes on to cover, not just the days modelled above, so every day is filtered against
                # its own weekday. rate_replicate() fills a missing minute from the same time of day 24
                # hours earlier and knows nothing about day_of_week, so a day left to it takes the
                # previous day's pattern - a weekend rule flattening the weekday peaks (batpred#5168).
                # Overrides (prev) already span the replicated horizon via max_minute.
                if not date and not prev:
                    stamp_end = max(max_minute + 24 * 60, self.forecast_minutes + 48 * 60)
                else:
                    stamp_end = max_minute

                # Store rates against range
                if end_minutes >= (-48 * 60) and start_minutes < max_minute:
                    for minute in range(start_minutes, end_minutes):
                        minute_mod = minute % max_minute
                        if (not date) or (minute >= (-24 * 60) and minute < max_minute):
                            minute_index = minute_mod - 24 * 60
                            # For incremental adjustments we have to loop over 24-hour periods
                            while minute_index < stamp_end:
                                if not date or (minute_index >= start_minutes and minute_index < end_minutes):
                                    # Floor division, so yesterday's minutes are checked against yesterday's weekday
                                    current_day_of_week = (day_of_week_midnight + minute_index // (24 * 60)) % 7
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
            else:
                self.log("Warn: Bad rate data provided in energy rates type {} {}".format(rtype, this_rate))

        return rates

    def rate_scan_export(self, rates, print=True):
        """
        Scan the rates and work out min/max
        """
        curr = self.currency_symbols[1]

        self.rate_export_min, self.rate_export_max, self.rate_export_average, self.rate_export_min_minute, self.rate_export_max_minute = self.rate_minmax(rates)
        if print:
            self.log("Export rates: min {}{}, max {}{}, average {}{}".format(self.rate_export_min, curr, self.rate_export_max, curr, self.rate_export_average, curr))

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

    def rate_base_min_max(self, rates):
        """
        Gap-fill `rates` into a "base" import curve (replicated, but without IO-slot/saving-session/
        override distortion) and work out its true min/max.

        Must scan the gap-filled curve, not the raw input: some tariffs (e.g. a fixed day/night
        product fetched with a short forward window - #4544) only have the currently-active segment
        in the raw data at fetch time, so a scan taken before replication can miss a cheaper/dearer
        segment that hasn't started yet and lock in the wrong "true" min/max for the rest of the day.
        rate_add_io_slots() then stamps IOG/SmartFlex dispatch slots with that stale value instead of
        the tariff's real off-peak price. Mirrors how rate_export_base is already built downstream of
        rate_replicate() on the export side.
        """
        rate_base, _ = self.rate_replicate(rates.copy(), {}, is_import=True, use_future=True)
        rate_min_base, rate_max_base, _, _, _ = self.rate_minmax(rate_base)
        return rate_base, rate_min_base, rate_max_base

    def rate_minmax_excluding_saving(self, rates, saving_minutes, rate_base):
        """
        Work out min/max/average over the forecast window at the tariff's own prices

        Each minute in saving_minutes (the minutes the saving/free/Axle loaders tagged, frozen before any
        override can overwrite the tag) is capped down to its rate in rate_base, which is `rates` without
        the session but with the same overrides. Capping only goes down, so an event reward is removed but
        a free or discounted session still counts as cheap. A session minute the tariff has no rate for
        is skipped. With no rate_base this is the plain min/max/average.
        """
        if not saving_minutes or not rate_base:
            return self.rate_minmax(rates)[:3]

        rate_min = 99999
        rate_max = 0
        rate_total = 0
        rate_n = 0

        for minute in range(self.minutes_now, self.forecast_minutes + self.minutes_now):
            if minute not in rates:
                continue
            rate = rates[minute]
            if minute in saving_minutes:
                if minute not in rate_base:
                    # A session created this minute (load_axle_slot() adds to rate_dict.get(minute, 0)),
                    # so the tariff has no rate of its own here and the synthetic reward is not one.
                    continue
                rate = min(rate, rate_base[minute])
            rate_min = min(rate_min, rate)
            rate_max = max(rate_max, rate)
            rate_total += rate
            rate_n += 1

        if rate_n == 0:
            return self.rate_minmax(rates)[:3]

        return dp2(rate_min), dp2(rate_max), dp2(rate_total / rate_n)

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

        # Work out the min rate going forward — O(n) right-to-left scan avoids O(n²) slice allocations
        running_min = rate_array[-1] if rate_array else self.rate_min
        for minute in range(len(rate_array) - 1, self.minutes_now - 1, -1):
            running_min = min(running_min, rate_array[minute])
            if minute < self.forecast_minutes + 24 * 60 + self.minutes_now:
                rate_min_forward[minute] = running_min

        self.log("Rate min forward looking: now {}{} at end of forecast {}{}".format(dp2(rate_min_forward[self.minutes_now]), self.currency_symbols[1], dp2(rate_min_forward[self.forecast_minutes]), self.currency_symbols[1]))

        return rate_min_forward

    def rate_scan_window(self, rates, rate_low_min_window, threshold_rate, find_high, return_raw=False, alt_rates=None, pv_light_dark=None, start_minute=0):
        """
        Scan for the next high/low rate window, from start_minute (midnight by default)
        """
        alt_rates = alt_rates or {}
        pv_light_dark = pv_light_dark or {}
        minute = start_minute
        found_rates = []
        lowest = 99
        highest = -99

        while True:
            rate_low_start, rate_low_end, rate_low_average = self.find_charge_window(rates, minute, threshold_rate, find_high, alt_rates=alt_rates, pv_light_dark=pv_light_dark)
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

    def find_low_rate_windows(self, alt_rates=None, pv_light_dark=None):
        """
        Scan the import rates for the low rate sensors' windows (low_rates_tariff) and the plan's charge windows (low_rates)

        Both start from rate_import_cost_threshold, which is worked out on the tariff's own prices so
        a saving session or Axle reward does not make the whole day read as cheap (GH#5050). In
        automatic mode, ahead of a saving session or Axle event priced above the tariff's highest import
        rate (see find_event_pre_charge()), the plan also gets every tariff import window before it starts,
        cut at the start, so it can charge the battery to export into the event or to cover the house
        through it (#249). An event whose import is raised is above the tariff maximum, so it is never
        offered as a charge window itself. The sensors, the car
        plan and the automatic threshold tightening keep the tariff's own cheap windows.
        """
        curr = self.currency_symbols[1]
        self.low_rates_tariff, lowest, highest = self.rate_scan_window(self.rate_import, 5, self.rate_import_cost_threshold, False, alt_rates=alt_rates, pv_light_dark=pv_light_dark)
        self.low_rates = self.low_rates_tariff
        self.log("Low Import rate found rates in range {}{} to {}{}".format(lowest, curr, highest, curr))

        event_start = self.rate_import_pre_event_end
        if event_start is not None and self.rate_import_pre_event_threshold > self.rate_import_cost_threshold:
            # Windows are scanned on a 5 minute grid from midnight, so the split is made at the grid step the event starts in.
            # Scanning only the minutes before it ends every pre-event window there; the boosted import of an earlier event
            # is above the tariff maximum, so it is never offered either.
            cut = event_start - event_start % 5
            before_event = {minute: rate for minute, rate in self.rate_import.items() if minute < cut}
            pre_event, _, _ = self.rate_scan_window(before_event, 5, self.rate_import_pre_event_threshold, False, alt_rates=alt_rates, pv_light_dark=pv_light_dark)
            if pre_event:
                after_event, _, _ = self.rate_scan_window(self.rate_import, 5, self.rate_import_cost_threshold, False, alt_rates=alt_rates, pv_light_dark=pv_light_dark, start_minute=cut)
                self.low_rates = pre_event + after_event
                self.log("Low Import rate: {} windows offered for charging ahead of the event at {}".format(len(pre_event), self.time_abs_str(event_start)))

        # Update threshold automatically, from the tariff's own windows so the published threshold does not
        # take in the pre-event day rate (GH#5050)
        if self.rate_low_threshold == 0 and highest >= self.rate_min:
            self.rate_import_cost_threshold = highest

    def find_event_pre_charge(self, tariff_import_max):
        """
        Find the start of the last saving session or Axle event worth charging ahead of, or None

        Each run of consecutive session minutes (import or export) is one event, and it starts at its first
        minute with an export or import price above the tariff's own highest import price - charging at any
        tariff rate before that can pay, either to export into the event or to avoid importing during it (a
        session on a 0p export tariff only raises the import side). Taking the first such minute rather than
        the run's first keeps a free session that runs straight into a saving session before the event,
        and a dip in price within an event does not split it. Free sessions and Axle import discounts lower
        the price, so never qualify. An event already running has nothing before it left to charge in, so
        it does not count. A manual threshold (rate_low_threshold) is the user's cap on what Predbat charges
        at, so this only applies in automatic mode.
        """
        if self.rate_low_threshold > 0:
            return None
        end_minute = self.minutes_now + self.forecast_minutes
        last_start = None
        block_start = None
        previous = None
        for minute in sorted(self.rate_import_saving_minutes | self.rate_export_saving_minutes):
            if minute >= end_minute:
                break
            if previous is None or minute != previous + 1:
                block_start = None
            previous = minute
            if block_start is None and (self.rate_export.get(minute, 0) > tariff_import_max or self.rate_import.get(minute, 0) > tariff_import_max):
                block_start = minute
                if minute > self.minutes_now:
                    last_start = minute
        return last_start

    def set_rate_thresholds(self):
        """
        Set the high and low rate thresholds

        The thresholds are worked out on the tariff's own prices: rate_minmax_excluding_saving() maps
        each saving-session/free-slot/Axle minute back to its tariff rate, so a two-rate tariff plus a
        session reward cannot make the whole ordinary-price day read as "low rate" (GH#5050).
        find_event_pre_charge() then finds any saving session or Axle event worth charging ahead of, which
        find_low_rate_windows() uses to give the plan the windows before it (#249).

        rate_import_tariff_max is kept for the PV10 "dispatch gone" price in prediction.py, which wants
        the tariff's worst price rather than an event reward. self.rate_min/rate_max/rate_average (and
        the export equivalents) are left untouched, since other consumers (dashboard sensors, graph
        scaling, plan.py pricing) want the real boosted price shown.
        """
        curr = self.currency_symbols[1]

        have_alerts = len(self.alert_active_keep) > 0
        have_manual_soc = len(self.manual_soc_keep) > 0

        car_planning_on_rates = self.num_cars > 0 and not self.octopus_intelligent_charging
        car_charging_max_price = max(self.car_charging_plan_max_price[: self.num_cars]) if car_planning_on_rates else 0.0

        # An empty table has nothing to filter, so keep the stored stats for that side as before - scanning
        # it would return the (99999, 0, 0) placeholder and push the threshold to 99998.9
        if self.rate_import:
            rate_min, rate_max, rate_average = self.rate_minmax_excluding_saving(self.rate_import, self.rate_import_saving_minutes, self.rate_import_pre_saving)
        else:
            rate_min, rate_max, rate_average = self.rate_min, self.rate_max, self.rate_average
        if self.rate_export:
            rate_export_min, rate_export_max, rate_export_average = self.rate_minmax_excluding_saving(self.rate_export, self.rate_export_saving_minutes, self.rate_export_pre_saving)
        else:
            rate_export_min, rate_export_max, rate_export_average = self.rate_export_min, self.rate_export_max, self.rate_export_average
        self.rate_import_tariff_max = rate_max

        if self.rate_low_threshold > 0:
            self.rate_import_cost_threshold = dp2(rate_average * self.rate_low_threshold)
        else:
            # In automatic mode select the only rate or everything but the most expensive
            if (rate_max == rate_min) or (rate_export_max > rate_max) or have_alerts or have_manual_soc:
                self.rate_import_cost_threshold = rate_max + 0.1
            else:
                self.rate_import_cost_threshold = rate_max - 0.5

        # When we plan car on the rates we need to include all the rates up to the max car price
        if car_planning_on_rates:
            self.rate_import_cost_threshold = max(self.rate_import_cost_threshold, car_charging_max_price + 0.1)

        # Ahead of an event worth charging for, every tariff slot is a candidate, as when export beats import above
        self.rate_import_pre_event_end = self.find_event_pre_charge(rate_max)
        self.rate_import_pre_event_threshold = rate_max + 0.1

        # Compute the export rate threshold
        if self.rate_high_threshold > 0:
            self.rate_export_cost_threshold = dp2(rate_export_average * self.rate_high_threshold)
        else:
            # In automatic mode select the only rate or everything but the most cheapest
            if (rate_export_max == rate_export_min) or (rate_export_min > rate_min) or have_alerts:
                self.rate_export_cost_threshold = rate_export_min - 0.1
            else:
                self.rate_export_cost_threshold = rate_export_min + 0.5

        self.log(
            "Rate thresholds (for charge/export) are import {}{} ({}{}), export {}{} ({}{})".format(
                dp2(self.rate_import_cost_threshold), curr, dp2(self.rate_low_threshold), curr, dp2(self.rate_export_cost_threshold), curr, dp2(self.rate_high_threshold), curr
            )
        )

    def rate_scan(self, rates, print=True):
        """
        Scan the rates and work out min/max
        """
        self.rate_min, self.rate_max, self.rate_average, self.rate_min_minute, self.rate_max_minute = self.rate_minmax(rates)
        # set_rate_thresholds() lowers this to the tariff's own maximum once it knows the event minutes
        self.rate_import_tariff_max = self.rate_max
        curr = self.currency_symbols[1]

        if print:
            # Calculate minimum forward rates only once rate replicate has run (when print is True).
            # Use the `rates` argument, not self.rate_import: during fetch's atomic rebuild self.rate_import
            # still holds the PREVIOUS cycle's data (publish is deferred to the end of the block), so
            # reading it here would make rate_min_forward - which drives plan.py charge/discharge economics
            # - a cycle stale. `rates` is the freshly-built current-cycle dict being scanned.
            self.rate_min_forward = self.rate_min_forward_calc(rates)
            self.log("Import rates: min {}{}, max {}{}, average {}{}".format(self.rate_min, curr, self.rate_max, curr, self.rate_average, curr))

    def rate_export_max_forward_calc(self, rates):
        """
        Work out the highest export rate from each minute forwards

        The export mirror of rate_min_forward_calc. Fed from the base export tariff (see
        rate_export_base) so a saving session cannot stand in for the tariff's general ability to sell
        surplus - that question is what the export haircut in battery_value_rate asks, and answering it
        with a one-off event price makes stored energy look fully recoverable on a system that in fact
        cannot sell a single kWh.
        """
        rate_array = []
        rate_export_max_forward = {}
        rate = 0.0

        for minute in range(self.forecast_minutes + self.minutes_now + 48 * 60):
            if minute in rates:
                rate = rates[minute]
            rate_array.append(rate)

        # Work out the max rate going forward — O(n) right-to-left scan avoids O(n²) slice allocations
        running_max = rate_array[-1] if rate_array else 0.0
        for minute in range(len(rate_array) - 1, self.minutes_now - 1, -1):
            running_max = max(running_max, rate_array[minute])
            if minute < self.forecast_minutes + 24 * 60 + self.minutes_now:
                rate_export_max_forward[minute] = running_max

        return rate_export_max_forward

    def rate_scan_gas(self, rates, print=True):
        """
        Scan the gas rates and work out min/max
        """
        self.rate_gas_min, self.rate_gas_max, self.rate_gas_average, self.rate_gas_min_minute, self.rate_gas_max_minute = self.rate_minmax(rates)
        curr = self.currency_symbols[1]

        if print:
            self.log("Gas rates: min {}{}, max {}{}, average {}{}".format(self.rate_gas_min, curr, self.rate_gas_max, curr, self.rate_gas_average, curr))

    def get_car_charging_now_tristate(self, car_n):
        """
        Read car_charging_now as a tri-state (True/False/None) for the #4516 Stage 1 dispatch
        timeline's GH#5080 reconciliation note.

        self.car_charging_now (set by get_car_charging_planned()) is a plain boolean used
        throughout the planner, where "no signal" and "confirmed not charging" both have to mean
        False - there is no room there for a genuine unknown. This diagnostic needs that
        distinction: flagging a slot as billing-risk when Predbat actually has no idea whether the
        car is charging would be a false positive on every dispatch, for every IOG user who hasn't
        configured the (optional) car_charging_now sensor, or during the "unknown"/"unavailable"
        state HA reports for a real sensor briefly after a restart.

        Returns None (unknown) when the sensor isn't configured, is out of range for this car, or
        HA is currently reporting "unknown"/"unavailable"; otherwise the normalised boolean.
        """
        if "car_charging_now" not in self.args:
            return None
        raw = self.get_arg("car_charging_now", "no", index=car_n)
        if raw is None:
            return None
        if isinstance(raw, str) and raw.lower() in ("unknown", "unavailable"):
            return None
        return self.car_charging_now[car_n]

    def car_charging_now_value(self, raw):
        """
        Interpret a car_charging_now reading: True (charging), False (not charging) or None (no evidence -
        unset, "unknown" or "unavailable").

        It can be an on/off sensor, matched against car_charging_now_response, or - for chargers with no
        "charging" sensor, e.g. Wallbox - a charging power sensor. A number is a power in watts (callers
        read it with required_unit="W", which converts kW from the entity's unit), and it is charging from
        CAR_CHARGING_NOW_POWER_W.
        """
        if raw is None or isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return raw >= CAR_CHARGING_NOW_POWER_W
        text = str(raw).strip().lower()
        if text in ("unknown", "unavailable"):
            return None
        try:
            return float(text) >= CAR_CHARGING_NOW_POWER_W
        except ValueError:
            return text in self.car_charging_now_response

    def car_plugged_in_now(self, car_n, arg="car_charging_planned"):
        """
        Read the car's plugged-in sensor arg: True plugged in, False unplugged, or None when it is
        unavailable or unknown. Matched against car_charging_planned_response.

        Reads the sensor afresh, so it can be used after fetch_sensor_data_cars() has replaced
        car_charging_planned with the Octopus dispatch state.
        """
        planned = self.get_arg(arg, "no", index=car_n)
        if isinstance(planned, bool):
            return planned
        if isinstance(planned, str):
            if planned.lower() in ("unavailable", "unknown"):
                return None
            return planned.lower() in self.car_charging_planned_response
        return False

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
        self.car_charging_max_rate = [0.0 for c in range(max(self.num_cars, 1))]
        self.car_charging_slots = [[] for c in range(self.num_cars)]
        self.car_charging_exclusive = [False for c in range(self.num_cars)]

        self.car_charging_planned_response = [str(response).lower() for response in self.get_arg("car_charging_planned_response", ["yes", "on", "enable", "true"])]
        self.car_charging_now_response = [str(response).lower() for response in self.get_arg("car_charging_now_response", ["yes", "on", "enable", "true", "charging"])]
        self.car_charging_from_battery = self.get_arg("car_charging_from_battery")

        # Car charging planned sensor
        for car_n in range(self.num_cars):
            # Get car N planned status - unavailable or unknown counts as not plugged in
            self.car_charging_planned[car_n] = bool(self.car_plugged_in_now(car_n))

            # Car is charging now sensor - an on/off sensor, or a charging power sensor read in watts
            self.car_charging_now[car_n] = bool(self.car_charging_now_value(self.get_arg("car_charging_now", "no", index=car_n, required_unit="W")))

            # Other car related configuration
            self.car_charging_plan_smart[car_n] = self.get_arg("car_charging_plan_smart", True)
            self.car_charging_plan_max_price[car_n] = self.get_arg("car_charging_plan_max_price", 0.0)
            self.car_charging_plan_time[car_n] = self.get_arg("car_charging_plan_time", "07:00:00")
            self.car_charging_battery_size[car_n] = dp2(float(self.get_arg("car_charging_battery_size", 100.0, index=car_n)))
            car_postfix = "" if car_n == 0 else "_" + str(car_n)
            self.car_charging_rate[car_n] = float(self.get_arg("car_charging_rate" + car_postfix))
            self.car_charging_max_rate[car_n] = self.car_max_rate_cap(car_n)
            self.car_max_rate_log_change(car_n, self.car_charging_max_rate[car_n])
            self.car_charging_rate[car_n] = self.car_rate_capped(car_n, self.car_charging_rate[car_n])
            self.car_charging_limit[car_n] = dp3((float(self.get_arg("car_charging_limit", 100.0, index=car_n)) * self.car_charging_battery_size[car_n]) / 100.0)
            self.car_charging_exclusive[car_n] = self.get_arg("car_charging_exclusive", False, index=car_n)
        self.car_max_rate_save()

        if self.num_cars > 0:
            car_charging_limit_percent = [dp1(limit / size * 100) if size else 0 for limit, size in zip(self.car_charging_limit, self.car_charging_battery_size)]
            self.log(
                "Cars {} charging from battery {} planned {}, charging_now {} smart {}, max_price {}{}, plan_time {}, battery size {}kWh, limit {}% ({}kWh), rate {}kW, exclusive {} (Predbat-led car settings, not Octopus Intelligent state)".format(
                    self.num_cars,
                    self.car_charging_from_battery,
                    self.car_charging_planned,
                    self.car_charging_now,
                    self.car_charging_plan_smart,
                    self.car_charging_plan_max_price,
                    self.currency_symbols[1],
                    self.car_charging_plan_time,
                    self.car_charging_battery_size,
                    car_charging_limit_percent,
                    self.car_charging_limit,
                    self.car_charging_rate,
                    self.car_charging_exclusive,
                )
            )

    def car_max_rate_entry(self, car_n):
        """
        Return car car_n's raw car_charging_max_rate entry (a number, an entity id, or None).

        A list is one entry per car, and a list shorter than num_cars is normal (the cars past its end
        are not capped), so only entries that exist are read - get_arg's index lookup would log an
        "Out of range index" warning every cycle for them. A single value, not a list, applies to
        every car.
        """
        entries = self.get_arg("car_charging_max_rate", None, indirect=False)
        if isinstance(entries, list):
            return entries[car_n] if car_n < len(entries) else None
        return entries

    def car_max_rate_cap(self, car_n):
        """
        Return car car_n's own maximum charge rate in kW from car_charging_max_rate, or 0.0 for no cap.

        Each entry is a number or an entity pointer. Absent, blank, 0, negative or a non-numeric fixed
        value means no cap. A pointed-to entity whose state is not a usable number - unknown,
        unavailable, none, blank (the states apps.yaml validation lets through) or any other text -
        holds the last usable cap for that car, so a sensor drop-out can not briefly put the car back
        on the charger's rate; text other than those states is warned about once per car. A pointer
        to an entity that does not exist is warned about once (again if it comes back and then goes
        away) and means no cap.
        """
        value = self.car_max_rate_entry(car_n)
        entity_id = value if is_entity_id(value) else None
        if entity_id:
            value = self.get_state_wrapper(entity_id, required_unit="kW")
            if value is None:
                if entity_id not in self.car_charging_max_rate_warned:
                    self.car_charging_max_rate_warned.add(entity_id)
                    self.log("Warn: car_charging_max_rate for car {} points to {} which does not exist, so the car's charge rate is not capped".format(car_n, entity_id))
                self.car_charging_max_rate_held.pop(car_n, None)
                return 0.0
            self.car_charging_max_rate_warned.discard(entity_id)
        try:
            cap = None if isinstance(value, bool) else float(value)
        except (ValueError, TypeError):
            cap = None
        if entity_id:
            if cap is None:
                # unknown/unavailable/none/blank is a normal drop-out; any other text is warned about once
                if str(value).strip().lower() not in ("unknown", "unavailable", "none", "") and car_n not in self.car_charging_max_rate_bad_warned:
                    self.car_charging_max_rate_bad_warned.add(car_n)
                    self.log("Warn: car_charging_max_rate for car {} reads {} = {}, which is not a number in kW, so the last cap is kept".format(car_n, entity_id, value))
                return self.car_charging_max_rate_held.get(car_n, 0.0)
            self.car_charging_max_rate_bad_warned.discard(car_n)
        if cap is None or cap != cap or cap <= 0:  # not a number, NaN or non-positive
            self.car_charging_max_rate_held.pop(car_n, None)
            return 0.0
        self.car_charging_max_rate_held[car_n] = cap
        return cap

    def car_max_rate_log_change(self, car_n, cap):
        """
        Log car car_n's car_charging_max_rate cap when it changes, rather than on every cycle.
        """
        if cap == self.car_charging_max_rate_logged.get(car_n, 0.0):
            return
        self.car_charging_max_rate_logged[car_n] = cap
        if cap > 0:
            self.log("Car {} maximum charge rate is {}kW (car_charging_max_rate); it plans at the lower of this and the charger rate".format(car_n, cap))
        else:
            self.log("Car {} charge rate no longer capped by car_charging_max_rate".format(car_n))

    def car_rate_capped(self, car_n, rate):
        """
        Return rate (kW) limited to car car_n's own maximum charge rate (car_charging_max_rate[car_n]).

        The charger's rate is only what the charger can deliver; a car's on-board charger can take
        less (e.g. a 3.6 kW plug-in hybrid on a 7.4 kW wallbox). A cap of 0 means no cap.
        """
        cap = self.car_charging_max_rate[car_n] if car_n < len(self.car_charging_max_rate) else 0.0
        if cap > 0 and rate > cap:
            return cap
        return rate

    def car_max_rate_save(self):
        """
        Save the held caps (car_charging_max_rate_held) to storage when they have changed since the last save,
        so a car whose cap sensor is not reporting yet after a restart keeps its last cap. Called every cycle,
        so it costs nothing when nothing has changed.
        """
        held = {str(car_n): cap for car_n, cap in self.car_charging_max_rate_held.items()}
        if held == self.car_charging_max_rate_saved:
            return
        storage = self.components.get_component("storage") if self.components else None
        if not storage:
            return
        from ha import run_async

        # Recorded whether or not the write works: a store that is failing is not retried every cycle
        self.car_charging_max_rate_saved = held
        try:
            if not run_async(storage.save("predbat", "car_max_rate", {"held": held}, format="json")):
                self.log("Warn: car_charging_max_rate: failed to save the held caps")
        except Exception as e:
            self.log("Warn: car_charging_max_rate: failed to save the held caps: {}".format(e))

    def car_max_rate_startup(self):
        """
        At start up, before the components start: restore the held caps saved by car_max_rate_save(), then
        work out each car's cap, so the first plan and Load ML's first fetch (a phase 2 component) both see them.
        """
        storage = self.components.get_component("storage") if self.components else None
        if storage:
            from ha import run_async

            try:
                data = run_async(storage.load("predbat", "car_max_rate"))
            except Exception as e:
                self.log("Warn: car_charging_max_rate: failed to load the held caps: {}".format(e))
                data = None
            held = data.get("held") if isinstance(data, dict) else None
            if not isinstance(held, dict):
                held = {}
            for key, cap in held.items():
                try:
                    car_n, cap = int(key), float(cap)
                except (ValueError, TypeError):
                    continue
                if cap > 0:
                    self.car_charging_max_rate_held[car_n] = cap
            self.car_charging_max_rate_saved = {str(car_n): cap for car_n, cap in self.car_charging_max_rate_held.items()}
        num_cars = min(self.get_arg("num_cars", 1), PREDBAT_MAX_CARS)
        self.car_charging_max_rate = [self.car_max_rate_cap(car_n) for car_n in range(max(num_cars, 1))]

    def fetch_ml_load_forecast(self, now_utc, save=True):
        """
        Fetches ML load forecast from sensor
        and returns it as a minute_data dictionary
        """
        # Use ML Model for load prediction
        load_ml_forecast = self.get_state_wrapper("sensor." + self.prefix + "_load_ml_forecast", attribute="results")
        if load_ml_forecast:
            if save:
                self.log_replay_ml_forecast(load_ml_forecast)
            self.log("Loading ML load forecast from sensor.{}_load_ml_forecast".format(self.prefix))
            # Convert format from dict to array
            if isinstance(load_ml_forecast, dict):
                data_array = []
                for key, value in load_ml_forecast.items():
                    data_array.append({"energy": value, "last_updated": key})

                # Load data
                load_forecast, _ = minute_data(
                    data_array,
                    self.forecast_days + 1,
                    self.midnight_utc,
                    "energy",
                    "last_updated",
                    backwards=False,
                    clean_increment=False,
                    smoothing=True,
                    divide_by=1.0,
                    scale=1.0,
                )

                if load_forecast:
                    self.log("Loaded the ML load forecast; load so far today {}kWh, load today at midnight {}kWh".format(dp1(load_forecast.get(self.minutes_now, 0)), dp1(load_forecast.get(24 * 60 - PREDICT_STEP, 0))))
                    return load_forecast
        return {}

    def fetch_ml_load_forecast_history(self, now_utc):
        """
        Reconstruct a genuine past Load ML forecast for the elapsed part of today.

        sensor.<prefix>_load_ml_stats publishes a load_today_h1 attribute every cycle: the model's
        cumulative-load-since-midnight prediction for 60 minutes after that cycle ran. Shifting each
        recorded reading's timestamp forward by that same 60-minute lead turns the entity's history
        into a genuine record of what the model predicted for each past target minute - unlike a
        same-time-of-day historical average, this is the model's own past output (batpred#4750).

        Covers 2 days of history so a reading from shortly before local midnight (whose target minute
        falls just after midnight) is included, rather than leaving the first hour of the day as a gap.
        """
        entity_id = "sensor." + self.prefix + "_load_ml_stats"
        history = self.get_history_wrapper(entity_id, days=2, required=False)
        if not history:
            return {}

        data_array = []
        for record in history[0]:
            attributes = record.get("attributes") or {}
            value = attributes.get("load_today_h1")
            last_updated = record.get("last_updated")
            if value is None or not last_updated:
                continue
            try:
                shifted_time = str2time(last_updated) + timedelta(minutes=60)
            except (ValueError, TypeError):
                continue
            data_array.append({"energy": value, "last_updated": shifted_time.isoformat()})

        if not data_array:
            return {}

        load_forecast, _ = minute_data(
            data_array,
            self.forecast_days + 1,
            self.midnight_utc,
            "energy",
            "last_updated",
            backwards=False,
            clean_increment=False,
            smoothing=True,
            divide_by=1.0,
            scale=1.0,
        )
        return load_forecast or {}

    def get_holiday_minutes(self, now_utc, num_days):
        """
        Build a per-minute history of the holiday_days_left value (indexed by minutes-ago) from the recorded
        entity history using minute_data, which holds each state forward until the next change.

        Covers num_days + 1 days rather than num_days: a sample taken "num_days whole days ago at time of
        day tod" reaches minutes_now past the num_days boundary, so a num_days map leaves the oldest day
        in the window unreadable for every slot with tod <= minutes_now (batpred#4732). This mirrors the
        load history window, which is deliberately max_days_previous = window_days + 1 for the same reason.
        Both the fetch and the minute_data span have to be widened - minute_data bounds its map at
        days * 24 * 60, so raising the index alone would just return the not-on-holiday default instead.

        Returns the minute_data dict, or None when no usable history is available.
        """
        item = self.config_index.get("holiday_days_left", None) if getattr(self, "config_index", None) else None
        entity_id = item.get("entity") if item else ("input_number." + self.prefix + "_holiday_days_left")
        if not entity_id:
            return None

        history_days = num_days + 1
        try:
            history = self.get_history_wrapper(entity_id=entity_id, days=history_days, required=False)
        except (ValueError, TypeError):
            history = None

        if not history or not isinstance(history, list) or not history[0]:
            return None

        holiday_minutes, _ = minute_data(history[0], history_days, now_utc, "state", "last_updated", backwards=True)
        return holiday_minutes or None

    def sample_load_history_bucket(self, minute_previous, holiday_minutes, max_holiday_index):
        """
        Read one 5-minute load history bucket (minute_previous minutes ago) for the weighted-bucket forecast.

        Returns (sample, holiday_active), or None for a zero (missing-data) bucket - those are skipped entirely
        so gaps in the history do not drag the estimate down. base_in_raw=False keeps raw at the true measured
        load so genuine gaps are detected even when a base load is configured (the sample still has it applied).

        holiday_active is the holiday state at the moment this individual sample was recorded. A sample older
        than the holiday history we hold is treated as not on holiday, the same default minute_data gives for a
        gap inside the window - treating it as matching instead would hand full weight to exactly the
        pre-holiday days holiday mode exists to discount.
        """
        sample, raw = self.get_filtered_load_minute(self.load_minutes, minute_previous, historical=False, step=PREDICT_STEP, base_in_raw=False)
        if raw <= 0:
            return None
        if holiday_minutes is None or minute_previous > max_holiday_index:
            holiday_active = False
        else:
            holiday_active = holiday_minutes.get(minute_previous, 0) > 0
        return sample, holiday_active

    def recorded_load_average(self, num_days, holiday_minutes, max_holiday_index):
        """
        Average 5-minute load over every bucket recorded in the search window, today so far included, kept apart
        by holiday state. Used to fill forecast slots whose time of day has never been recorded (batpred#5548).

        Returns {holiday_active: average kWh per 5 minutes} for each state with at least
        LOAD_FORECAST_RECORDED_MIN_MINUTES of recorded load, so the first few minutes after an install cannot
        set the whole forecast on their own.
        """
        totals = {False: 0.0, True: 0.0}
        counts = {False: 0, True: 0}
        for minute_previous in range(0, (num_days + 1) * 24 * 60, PREDICT_STEP):
            bucket = self.sample_load_history_bucket(minute_previous, holiday_minutes, max_holiday_index)
            if bucket is None:
                continue
            sample, holiday_active = bucket
            totals[holiday_active] += sample
            counts[holiday_active] += 1
        min_buckets = LOAD_FORECAST_RECORDED_MIN_MINUTES // PREDICT_STEP
        return {state: totals[state] / counts[state] for state in totals if counts[state] >= min_buckets}

    def compute_load_forecast_history(self, now_utc):
        """
        Build a forward load estimate from all available history (up to LOAD_FORECAST_HISTORY_MAX_DAYS days)
        using per-5-minute weighted-bucket averaging.

        For each forward 5-minute slot the historical sample at the same time-of-day is gathered from each
        available past day and combined as a weighted average, ignoring zero (missing-data) buckets entirely.

        Only days whose holiday state matches that of the day the slot falls on are averaged - the holiday
        state is matched per sample (not per whole day) so a mid-day change of holiday mode is handled
        correctly, and per forward day (not just today's) so the day you come home is planned against
        non-holiday history. Per-sample weight = weekday_factor * age_factor:
          - weekday_factor: 1.0 if the historical day is the same weekday as today; else 0.7 if both are
            weekend or both are weekday; else 0.5 (one weekday, one weekend). Held at 1.0 between two
            holiday days: holiday load has no weekday structure, and discarding weight from a pool of two
            or three matching days only amplifies noise.
          - age_factor: 0.9 for yesterday, reducing by 0.03 per day down to a floor of 0.1.

        A slot with no matching history at all - the first 24 hours of a holiday, or a return from one
        longer than the search window - falls back to the plain weighted average of every day scaled by
        holiday_load_scaling (or divided by it when coming home), so holiday mode acts from the moment it
        is switched on rather than waiting for a day of holiday history to accumulate (batpred#4732).

        A slot with no recorded sample on any previous day - a new install whose history starts part-way
        through yesterday, or today - is filled from today instead (batpred#5548): today's sample at the same
        time of day when that time has already passed, otherwise the average of everything recorded so far
        once at least LOAD_FORECAST_RECORDED_MIN_MINUTES exist, otherwise base load. Elapsed slots of today
        with nothing recorded (before the install) stay at base load, as that is what load_today_comparison
        counts as their actual load - the average there would drag the in-day adjustment down. Today never contributes
        while a previous day has data for the slot, so once a full day is recorded these fall away and an
        established install is unaffected. Today's elapsed slots read today's own load, which keeps the in-day
        adjustment neutral rather than letting it scale up a forecast that is already built from today.

        Returns a cumulative-from-midnight kWh dict (same format as the ML load forecast), or {} if no data.
        """
        if not self.load_minutes:
            return {}
        # Search window comes from max_days_previous (derived from days_previous), bounded by available history.
        # load_minutes_age counts whole 24-hour periods, so it is still 0 at 14:00 for a 20:00 install yesterday;
        # yesterday is always searched so its recorded evening is used (unrecorded buckets are skipped anyway).
        num_days = max(min(max(self.load_minutes_age, 1), self.max_days_previous - 1), 0)

        today_dow = now_utc.weekday()
        holiday_minutes = self.get_holiday_minutes(now_utc, num_days)
        # get_holiday_minutes covers num_days + 1 days so the oldest day in the window stays readable for
        # every slot; anything older than that is genuinely unknown (a purged or short recorder history)
        max_holiday_index = (num_days + 1) * 24 * 60 - 1
        # Assumed holiday load as a fraction of normal, used only for slots with no matching history
        holiday_load_scaling = min(max(self.holiday_load_scaling, 0.1), 1.0)

        # Precompute the per-day weights; the holiday state is matched per 5-minute bucket below. The age-only
        # weight is what a holiday-to-holiday match uses, the weekday * age weight everything else.
        day_static_weight = {}
        day_age_weight = {}
        for d in range(1, num_days + 1):
            hist_dow = (now_utc - timedelta(days=d)).weekday()
            if hist_dow == today_dow:
                weekday_factor = 1.0
            elif (hist_dow >= 5) == (today_dow >= 5):
                # Both weekend or both weekday
                weekday_factor = 0.7
            else:
                weekday_factor = 0.5
            # Age: 0.9 for yesterday (d=1), reducing by 0.03 per day down to a floor of 0.1
            age_factor = max(0.1, 0.9 - (d - 1) * 0.03)
            day_age_weight[d] = age_factor
            day_static_weight[d] = weekday_factor * age_factor

        # Build the per-step (5-minute) weighted-average estimate, keyed by minute-from-midnight, across the
        # whole horizon from midnight today through the end of the plan so the resulting array is genuinely
        # cumulative-from-midnight (consumers such as load_today_comparison read minutes before minutes_now too).
        horizon_end = self.minutes_now + self.forecast_minutes + self.plan_interval_minutes
        per_step = {}
        has_history = False
        today_slots = 0
        average_slots = 0
        recorded_average = None  # built on first use - only slots no previous day covers need it
        base_step = self.base_load * PREDICT_STEP / 60.0
        for minute_absolute in range(0, horizon_end, PREDICT_STEP):
            tod = minute_absolute % (24 * 60)  # time of day of this slot
            # Holiday state of the day this slot falls on, not of today. holiday_days_left counts whole days
            # remaining including today and is decremented at midnight, so the day you travel home (and every
            # day after it) is planned against non-holiday history while you are still away.
            slot_holiday = self.holiday_days_left > (minute_absolute // (24 * 60))
            match_total = 0.0
            match_weight = 0.0
            all_total = 0.0
            all_weight = 0.0
            for d in range(1, num_days + 1):
                # Sample d whole days ago at this slot's time of day. Counting in whole days from today keeps
                # each day distinct and handles midnight crossings (the slot may be tomorrow or later).
                minute_previous = (self.minutes_now - tod) + d * 24 * 60
                bucket = self.sample_load_history_bucket(minute_previous, holiday_minutes, max_holiday_index)
                if bucket is None:
                    continue
                sample, holiday_active = bucket
                all_total += sample * day_static_weight[d]
                all_weight += day_static_weight[d]
                if holiday_active == slot_holiday:
                    weight = day_age_weight[d] if slot_holiday else day_static_weight[d]
                    match_total += sample * weight
                    match_weight += weight
            if all_weight == 0 and tod <= self.minutes_now:
                # No previous day recorded this time of day, but today already has (new install, batpred#5548).
                # A time of day still ahead of us today would be a future minute, so it is never read.
                bucket = self.sample_load_history_bucket(self.minutes_now - tod, holiday_minutes, max_holiday_index)
                if bucket is not None:
                    sample, holiday_active = bucket
                    all_total, all_weight = sample, 1.0
                    if holiday_active == slot_holiday:
                        match_total, match_weight = sample, 1.0
                    today_slots += 1
            if match_weight > 0:
                per_step[minute_absolute] = match_total / match_weight
                has_history = True
            elif all_weight > 0:
                # Nothing in the window shares this slot's holiday state. Rather than leaving holiday mode inert
                # (a normalised mean cannot express "all of my data is wrong"), fall back to every day scaled by
                # the assumed holiday ratio - or divided by it when we are home and the whole window was holiday.
                per_step[minute_absolute] = (all_total / all_weight) * (holiday_load_scaling if slot_holiday else 1.0 / holiday_load_scaling)
                has_history = True
            elif minute_absolute < self.minutes_now:
                # Elapsed today with nothing recorded (before the install). load_today_comparison counts these as
                # base load actual, so forecasting the recorded average here would read as a large over-forecast
                # and pull the in-day adjustment - and through it tonight's plan - down.
                per_step[minute_absolute] = base_step
            else:
                # Nothing recorded at this time of day yet: project the average of everything recorded so far,
                # with the same holiday matching and scaling as above, or base load until there is enough of it.
                if recorded_average is None:
                    recorded_average = self.recorded_load_average(num_days, holiday_minutes, max_holiday_index)
                if slot_holiday in recorded_average:
                    per_step[minute_absolute] = recorded_average[slot_holiday]
                elif (not slot_holiday) in recorded_average:
                    per_step[minute_absolute] = recorded_average[not slot_holiday] * (holiday_load_scaling if slot_holiday else 1.0 / holiday_load_scaling)
                else:
                    per_step[minute_absolute] = base_step
                    continue
                has_history = True
                average_slots += 1

        if not has_history:
            # Nothing recorded at all - leave the forecast to the days_previous fallback
            return {}
        if today_slots or average_slots:
            self.log("Load forecast: no previous-day history for some times of day, {} slots filled from today's load at the same time and {} from the average of all recorded load".format(today_slots, average_slots))

        # Convert per-step kWh buckets into a cumulative-from-midnight dict, filling every minute by linear
        # interpolation across each 5-minute span so get_from_incrementing(..., backwards=False) reproduces
        # the per-step energy exactly (mirroring the dense output of minute_data with smoothing).
        load_forecast = {}
        cumulative = 0.0
        for minute_absolute in range(0, horizon_end, PREDICT_STEP):
            step_energy = per_step[minute_absolute]
            for offset in range(PREDICT_STEP):
                load_forecast[minute_absolute + offset] = dp4(cumulative + step_energy * offset / PREDICT_STEP)
            cumulative += step_energy
        # Final boundary so the last span's increment is well defined
        load_forecast[horizon_end] = dp4(cumulative)
        return load_forecast

    def apply_load_ml_forecast_history(self, now_utc, ml_forecast=None):
        """
        Backfill the elapsed part of today with genuine past Load ML predictions.

        Only takes effect when Load ML is genuinely the active forecast source this cycle, which is
        exactly "fetch_sensor_data() fetched a non-empty ml_forecast this cycle" - a Load ML component
        running in the background without being selected as the forecast source
        (load_ml_enable/load_ml_source) must not influence the chart, so it has no effect and the
        weighted-bucket historical forecast above (skipped while Load ML owns load_forecast_only) is
        what would apply instead. This is passed in rather than held on self so there is no cycle-scoped
        flag whose correctness depends on fetch_config_options() always running first (#4762 review).
        Load ML's own forecast entity only ever publishes predictions from "now" onward
        (load_ml_component.py _publish_entity), so without this the already-elapsed part of today has no
        forecast data at all and load_today_comparison's Predicted chart series sits at zero from
        midnight until "now" (batpred#4750).
        """
        if not ml_forecast:
            return
        h1_forecast = self.fetch_ml_load_forecast_history(now_utc)
        for minute, value in h1_forecast.items():
            if minute < self.minutes_now:
                self.load_forecast[minute] = value

    def fetch_extra_load_forecast(self, now_utc, ml_forecast=None):
        """
        Fetch extra load forecast, this is future load data
        """
        load_forecast_final = {}
        load_forecast_array = []

        # Add ML forecast if available
        if ml_forecast:
            load_forecast_array.append(ml_forecast)

        if "load_forecast" in self.args:
            entity_ids = self.get_arg("load_forecast", indirect=False)
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]

            for entity_id in entity_ids:
                if not entity_id:
                    self.log("Warn: Unable to fetch load forecast data, check your load_forecast setting in apps.yaml")
                    continue

                attribute = None
                if "$" in entity_id:
                    entity_id, attribute = entity_id.split("$")
                try:
                    self.log("Loading extra load forecast from {}, attribute {}".format(entity_id, attribute))
                    data = self.get_state_wrapper(entity_id=entity_id, attribute=attribute)
                except (ValueError, TypeError) as e:
                    self.log("Error: Unable to fetch load forecast data from sensor {}, exception {}".format(entity_id, e))
                    data = None

                # Convert format from dict to array
                if isinstance(data, dict):
                    data_array = []
                    for key, value in data.items():
                        data_array.append({"energy": value, "last_updated": key})
                    data = data_array

                # Load data
                load_forecast, _ = minute_data(
                    data,
                    self.forecast_days + 1,
                    self.midnight_utc,
                    "energy",
                    "last_updated",
                    backwards=False,
                    clean_increment=False,
                    smoothing=True,
                    divide_by=1.0,
                    scale=1.0,
                    required_unit="kWh",
                )

                if load_forecast:
                    self.log("Loaded the load forecast from {} load sensor; from midnight {}kWh to now {}kWh to midnight {}kwh".format(entity_id, load_forecast.get(0, 0), load_forecast.get(self.minutes_now, 0), load_forecast.get(24 * 60, 0)))
                    load_forecast_array.append(load_forecast)
                else:
                    self.log("Warn: Unable to load the load forecast from {}. Skipping forecast source.".format(entity_id))

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
                carbon_data, _ = minute_data(data_all, self.forecast_days, self.now_utc, "intensity", "from", backwards=False, to_key="to")
                carbon_data, carbon_replicated = self.carbon_replicate(carbon_data)
                if carbon_replicated:
                    self.log("Warn: Carbon intensity forecast only covers {} hours of the {} hour plan, replicating it forward to cover the rest".format(dp1(min(carbon_replicated) / 60), dp1(self.forecast_minutes / 60)))

        entity_id = self.prefix + ".carbon_now"
        state = self.get_state_wrapper(entity_id=entity_id)
        if state is not None:
            try:
                carbon_history = self.minute_data_import_export(self.max_days_previous, self.now_utc, entity_id, required_unit="g/kWh", increment=False, smoothing=False)
            except (ValueError, TypeError):
                self.log("Warn: No carbon intensity history in sensor {}".format(entity_id))
        else:
            self.log("Warn: No carbon intensity history in sensor {}".format(entity_id))

        return carbon_data, carbon_history

    def validate_curve(self, curve, curve_name):
        """
        Validate a power or temperature curve dictionary
        Ensures all keys are integers and all values are floats

        Args:
            curve: Dictionary to validate
            curve_name: Name of the curve for logging

        Returns:
            Validated dictionary with integer keys and float values
        """
        if not isinstance(curve, dict):
            self.log("Warn: {} is incorrectly configured - ignoring".format(curve_name))
            self.record_status("Warn: {} is incorrectly configured - ignoring".format(curve_name), had_errors=True)
            return {}

        validated_curve = {}
        for key in curve:
            try:
                int_key = int(key)
                value = float(curve[key])
                validated_curve[int_key] = value
            except (ValueError, TypeError):
                self.log("Warn: {} has bad key/value for key {} - ignoring".format(curve_name, key))
                self.record_status("Warn: {} has bad key/value for key {} - ignoring".format(curve_name, key), had_errors=True)

        return validated_curve

    def check_export_more_solar_effective(self):
        """
        Warn when export_more_solar is on but cannot have any effect.

        export_more_solar works by enabling Freeze Export on idle solar slots, and optimise_solar() returns
        immediately unless export windows are being calculated and set_export_freeze is on. Otherwise the
        switch is on but does nothing, with no indication anywhere (#4865). Must run after
        fetch_inverter_data(), which forces set_export_freeze off for an inverter without freeze support,
        so the value read in fetch_config_options() is not yet the one the plan will use.

        Warns once per incident rather than every cycle, as the combination may be deliberate, but again
        whenever the reason changes - otherwise fixing one cause would silently leave the next one in place.
        Re-arms once export_more_solar is effective so a later recurrence is reported.
        """
        reason = None
        if self.export_more_solar:
            if not self.battery_export_permitted:
                reason = "the inverter is not permitted to export battery energy, so export slots are not planned. Turn export_more_solar off."
            elif not self.calculate_best_export:
                reason = "Predbat mode is not Control charge & discharge, so export slots are not planned. Change the mode, or turn export_more_solar off."
            elif not self.set_export_freeze:
                # Ask the inverter rather than the switch: an unsupported inverter forces set_export_freeze off
                # whatever the switch says, so advising the user to turn the switch on would change nothing.
                if any(not inverter.inv_support_discharge_freeze for inverter in self.inverters):
                    reason = "the inverter does not support Freeze Export. Turn export_more_solar off."
                else:
                    reason = "set_export_freeze is off. Enable set_export_freeze, or turn export_more_solar off."

        if reason and reason != self.export_more_solar_warned_reason:
            self.log("Warn: export_more_solar is enabled but has no effect, as it works by enabling Freeze Export on idle solar slots and " + reason)
        self.export_more_solar_warned_reason = reason

    def fetch_config_options(self):
        """
        Fetch all the configuration options
        """

        self.debug_enable = self.get_arg("debug_enable")
        self.plan_debug = self.get_arg("plan_debug")
        self.previous_status = self.get_state_wrapper(self.prefix + ".status")
        forecast_hours = max(self.get_arg("forecast_hours", 48), 24)

        self.num_cars = self.get_arg("num_cars", 1)
        if self.num_cars > PREDBAT_MAX_CARS:
            self.log("Warn: num_cars {} exceeds the {} cars Predbat supports - clamping to {}".format(self.num_cars, PREDBAT_MAX_CARS, PREDBAT_MAX_CARS))
            self.record_status("Warn: num_cars {} clamped to the maximum of {} supported cars".format(self.num_cars, PREDBAT_MAX_CARS), had_errors=True)
            self.num_cars = PREDBAT_MAX_CARS
        self.calculate_plan_every = max(self.get_arg("calculate_plan_every"), 5)
        self.calculate_savings_max_charge_slots = self.get_arg("calculate_savings_max_charge_slots", 1)

        self.log("Configuration: forecast_hours {}, num_cars {}, debug enable is {}, calculate_plan_every {} minutes".format(forecast_hours, self.num_cars, self.debug_enable, self.calculate_plan_every))

        # Days previous
        self.holiday_days_left = self.get_arg("holiday_days_left")
        self.holiday_load_scaling = self.get_arg("holiday_load_scaling", 0.7)
        self.load_forecast_only = self.get_arg("load_forecast_only", False)

        self.days_previous = self.get_arg("days_previous", [7])
        self.days_previous_weight = self.get_arg("days_previous_weight", [1 for i in range(len(self.days_previous))])
        if len(self.days_previous) > len(self.days_previous_weight):
            # Extend weights with 1 if required
            self.days_previous_weight += [1 for i in range(len(self.days_previous) - len(self.days_previous_weight))]

        # days_previous_auto enables the weighted-bucket historical load forecast. The number of days of history
        # it searches comes from max(days_previous) (or 7 when days_previous is not set), capped to the history
        # Predbat can hold (LOAD_FORECAST_HISTORY_MAX_DAYS). Enabled by default.
        self.load_forecast_history = self.get_arg("days_previous_auto", True)
        if self.load_forecast_history:
            window_days = min(max(self.days_previous) if self.days_previous else 7, LOAD_FORECAST_HISTORY_MAX_DAYS)
            # Config-time log only - describes what's enabled, not what happened this cycle. This
            # runs unconditionally every cycle regardless of whether the weighted-bucket forecast
            # actually gets used: Load ML (or any other source that sets load_forecast_only) takes
            # precedence and skips it entirely (fetch_sensor_data(), guarded by
            # "not self.load_forecast_only") - apply_load_ml_forecast_history() backfills the
            # elapsed part of today from Load ML's own history instead in that case (batpred#4750).
            # The "using weighted-bucket..." wording previously here read as if it was happening
            # every cycle regardless, which is what actually gets logged only when the forecast is
            # genuinely used (fetch_sensor_data()'s own "Using weighted-bucket historical load
            # forecast over N days" line) - confusing on a Load ML setup where this fallback is
            # rarely/never actually invoked (#4496 follow-up).
            self.log("days_previous_auto enabled - will fall back to a weighted-bucket historical load forecast over up to {} days if no other load forecast source takes precedence".format(window_days))
            self.max_days_previous = window_days + 1
        elif self.holiday_days_left > 0:
            self.days_previous = [1]
            self.log("Holiday mode is active, {} days remaining, setting days previous to 1".format(self.holiday_days_left))
            self.max_days_previous = max(self.days_previous) + 1
        else:
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
        self.metric_dynamic_load_adjust = self.get_arg("metric_dynamic_load_adjust")
        self.rate_low_threshold = self.get_arg("rate_low_threshold")
        self.rate_high_threshold = self.get_arg("rate_high_threshold")
        self.inverter_soc_reset = self.get_arg("inverter_soc_reset")

        self.metric_battery_value_scaling = self.get_arg("metric_battery_value_scaling")
        self.metric_battery_value_export_scaling = self.get_arg("metric_battery_value_export_scaling")
        self.notify_devices = self.get_arg("notify_devices", ["notify"])
        self.pv_scaling = self.get_arg("pv_scaling")
        self.pv_metric10_weight = self.get_arg("pv_metric10_weight")
        self.calculate_pv90_plan = self.get_arg("calculate_pv90_plan")
        self.pv_metric90_weight = self.get_arg("pv_metric90_weight")
        self.load_scaling = self.get_arg("load_scaling")
        self.load_scaling10 = self.get_arg("load_scaling10")
        self.load_scaling90 = self.get_arg("load_scaling90")
        if not self.calculate_pv90_plan:
            # calculate_pv90_plan (CHANGE 2) gates the whole feature: pv_metric90_weight's own
            # CONFIG_ITEMS default is 0.15, not 0.0, so the user sees a non-zero weight in Home
            # Assistant even while the switch is off. Force the runtime weight back to 0.0 here so
            # every one of the pre-existing `pv_metric90_weight > 0` gates elsewhere (the pv90 launch
            # paths in plan.py, and the pv90 term collapsing out of compute_metric) stays inert until
            # the user explicitly turns the switch on - no separate gating on calculate_pv90_plan is
            # added anywhere else, this is the single choke point.
            self.pv_metric90_weight = 0.0

        self.charge_scaling10 = self.get_arg("charge_scaling10")
        self.load_scaling_saving = self.get_arg("load_scaling_saving")
        self.load_scaling_free = self.get_arg("load_scaling_free")
        self.battery_rate_max_scaling = self.get_arg("battery_rate_max_scaling")
        self.battery_rate_max_scaling_discharge = self.get_arg("battery_rate_max_scaling_discharge")

        self.best_soc_step = 0.25
        self.metric_cloud_enable = self.get_arg("metric_cloud_enable")
        self.metric_load_divergence_enable = self.get_arg("metric_load_divergence_enable")

        # Battery charging options
        self.battery_capacity_nominal = self.get_arg("battery_capacity_nominal")
        self.battery_scaling_auto = self.get_arg("battery_scaling_auto", default=False)
        self.battery_loss = 1.0 - self.get_arg("battery_loss")
        self.battery_loss_discharge = 1.0 - self.get_arg("battery_loss_discharge")
        self.inverter_loss = 1.0 - self.get_arg("inverter_loss")
        self.inverter_hybrid = self.get_arg("inverter_hybrid")
        self.pv_ac_limit = self.get_arg("pv_ac_limit", 0.0) / MINUTE_WATT
        self.inverter_freeze_export_discharge_rate = max(self.get_arg("inverter_freeze_export_discharge_rate", 0.0), 0.0) / MINUTE_WATT
        self.log("Freeze Export discharge rate configured: {:.0f} W".format(self.inverter_freeze_export_discharge_rate * MINUTE_WATT))
        self.base_load = self.get_arg("base_load", 100) / 1000.0

        # Charge curve
        if self.args.get("battery_charge_power_curve", "") == "auto":
            self.battery_charge_power_curve_auto = True
        else:
            self.battery_charge_power_curve_auto = False
            self.battery_charge_power_curve = self.validate_curve(self.args.get("battery_charge_power_curve", {}), "battery_charge_power_curve")
        self.battery_charge_power_curve_default = self.validate_curve(self.args.get("battery_charge_power_curve_default", {}), "battery_charge_power_curve_default")

        # Discharge curve
        if self.args.get("battery_discharge_power_curve", "") == "auto":
            self.battery_discharge_power_curve_auto = True
        else:
            self.battery_discharge_power_curve_auto = False
            self.battery_discharge_power_curve = self.validate_curve(self.args.get("battery_discharge_power_curve", {}), "battery_discharge_power_curve")
        self.battery_discharge_power_curve_default = self.validate_curve(self.args.get("battery_discharge_power_curve_default", {}), "battery_discharge_power_curve_default")

        # Temperature curve charge/discharge
        self.battery_temperature_charge_curve = self.validate_curve(self.args.get("battery_temperature_charge_curve", {}), "battery_temperature_charge_curve")
        self.battery_temperature_discharge_curve = self.validate_curve(self.args.get("battery_temperature_discharge_curve", {}), "battery_temperature_discharge_curve")

        self.import_export_scaling = self.get_arg("import_export_scaling", 1.0)
        self.best_soc_min = self.get_arg("best_soc_min")
        self.best_soc_max = self.get_arg("best_soc_max")
        self.best_soc_keep = self.get_arg("best_soc_keep")
        self.best_soc_keep_weight = self.get_arg("best_soc_keep_weight")
        self.set_soc_minutes = self.plan_interval_minutes
        self.set_window_minutes = self.plan_interval_minutes
        self.inverter_set_charge_before = self.get_arg("inverter_set_charge_before")
        if not self.inverter_set_charge_before:
            self.set_soc_minutes = 0
            self.set_window_minutes = 0
        self.octopus_intelligent_charging = self.get_arg("octopus_intelligent_charging")
        self.octopus_intelligent_ignore_unplugged = self.get_arg("octopus_intelligent_ignore_unplugged")
        self.octopus_intelligent_consider_full = self.get_arg("octopus_intelligent_consider_full")
        self.octopus_intelligent_trust_slots = self.get_arg("octopus_intelligent_trust_slots")
        self.octopus_intelligent_dynamic = self.get_arg("octopus_intelligent_dynamic")
        self.car_energy_reported_load = self.get_arg("car_energy_reported_load")
        self.get_car_charging_planned()
        self.dynamic_load_car_check_config()
        self.load_inday_adjustment = 1.0

        self.combine_rate_threshold = self.get_arg("combine_rate_threshold")
        self.combine_export_slots = self.get_arg("combine_export_slots")
        self.combine_charge_slots = self.get_arg("combine_charge_slots")
        self.charge_slot_split = self.plan_interval_minutes
        self.export_slot_split = self.plan_interval_minutes
        self.calculate_best = True
        self.set_read_only = self.get_arg("set_read_only")
        self.set_read_only_axle = False

        # Check if Axle control is enabled and an event is active
        if self.get_arg("axle_control", False) and not self.set_read_only:
            if fetch_axle_active(self):
                self.log("Axle VPP event is active - enabling read-only mode")
                self.set_read_only = True
                self.set_read_only_axle = True

        # hard wired options, can be configured per inverter later on
        self.set_soc_enable = True
        self.set_reserve_enable = self.get_arg("set_reserve_enable")
        self.set_reserve_hold = True
        self.set_export_freeze = self.get_arg("set_export_freeze")
        self.set_charge_freeze = self.get_arg("set_charge_freeze")
        self.set_charge_freeze_only = self.get_arg("set_charge_freeze_only")
        self.set_charge_low_power = self.get_arg("set_charge_low_power")
        self.set_export_low_power = self.get_arg("set_export_low_power")
        self.charge_low_power_margin = self.get_arg("charge_low_power_margin")
        self.low_power_pv_threshold_w = self.get_arg("low_power_pv_threshold_w")
        self.set_charge_low_power_solar_full_rate = self.get_arg("set_charge_low_power_solar_full_rate")
        self.calculate_export_first = True

        self.set_status_notify = self.get_arg("set_status_notify")
        self.set_inverter_notify = self.get_arg("set_inverter_notify")
        self.set_export_freeze_only = self.get_arg("set_export_freeze_only")
        self.set_discharge_during_charge = self.get_arg("set_discharge_during_charge")
        self.set_freeze_export_during_demand = self.get_arg("set_freeze_export_during_demand")
        self.export_more_solar = self.get_arg("export_more_solar")
        self.export_more_solar_threshold = self.get_arg("export_more_solar_threshold")
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
        self.calculate_export_on_pv = self.get_arg("calculate_export_on_pv")
        self.calculate_second_pass = self.get_arg("calculate_second_pass")
        self.prediction_kernel_enable = self.get_arg("prediction_kernel_enable", True)
        self.calculate_inday_adjustment = self.get_arg("calculate_inday_adjustment")
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
            self.log("Info: Read-only mode is enabled, the inverter controls will not be used!!")

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
        if not self.car_energy_reported_load:
            # If car energy is not reported as load then we should not attempt to remove car energy from the load data.
            self.car_charging_hold = False
        self.car_manual_soc_config()
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
        self.manual_load_adjust = self.manual_rates("manual_load_adjust", default_rate=self.get_arg("manual_load_value"))
        self.manual_soc_keep = self.manual_rates("manual_soc", default_rate=self.get_arg("manual_soc_value"))
        self.manual_soc_max_keep = self.manual_rates("manual_soc_max", default_rate=self.get_arg("manual_soc_max_value"))

        # Update list of config options to save/restore to
        self.update_save_restore_list()

    def load_car_energy(self, now_utc):
        """
        Load previous car charging energy data
        """
        self.car_charging_energy = {}
        if "car_charging_energy" in self.args:
            self.car_charging_energy = self.minute_data_import_export(self.max_days_previous, now_utc, "car_charging_energy", scale=self.car_charging_energy_scale, required_unit="kWh")
            if self.car_charging_hold and not self.car_charging_energy:
                # car_charging_energy is configured (an entity is set) but resolved to nothing at
                # all - most likely the entity no longer exists (e.g. removed upstream, see #4458),
                # though it can also happen for a recorder-excluded entity or a fresh install with
                # no history yet, which aren't necessarily broken and can persist indefinitely.
                # minute_data_import_export() already logs a benign "Unable to fetch history"
                # warning per entity with no had_errors flag, so this degrades to the
                # car_charging_threshold heuristic below completely silently otherwise. Log the
                # loud warning only once per incident (not every cycle - see PR review on #4469)
                # so a persistent-but-intentional setup doesn't get spammed every 5 minutes; the
                # status sensor still reflects the degraded state every cycle via record_status.
                if not self.car_charging_energy_warned:
                    self.log(
                        "Warn: car_charging_hold is enabled and car_charging_energy is configured ({}) but no data could be loaded for it - falling back to the car_charging_threshold heuristic instead of precise car energy subtraction. Check the entity still exists.".format(
                            self.get_arg("car_charging_energy", indirect=False)
                        )
                    )
                    self.car_charging_energy_warned = True
                self.record_status("Warn: car_charging_energy entity has no data, falling back to car_charging_threshold heuristic", had_errors=True)
            else:
                self.car_charging_energy_warned = False
        else:
            self.log("Car charging hold {}, threshold {}kWh".format(self.car_charging_hold, self.car_charging_threshold * 60.0))
        return self.car_charging_energy
