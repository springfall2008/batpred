# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import timedelta
from config import PREDICT_STEP, RUN_EVERY, TIME_FORMAT
from utils import remove_intersecting_windows, get_charge_rate_curve, get_discharge_rate_curve, find_charge_rate, calc_percent_limit, in_iboost_slot, in_car_slot


# Only assign globals once to avoid re-creating them with processes are forked
if not "PRED_GLOBAL" in globals():
    PRED_GLOBAL = {}


def reset_prediction_globals():
    global PRED_GLOBAL
    PRED_GLOBAL = {}


def wrapped_run_prediction_single(charge_limit, charge_window, export_window, export_limits, pv10, end_record, step):
    global PRED_GLOBAL
    pred = Prediction()
    pred.__dict__ = PRED_GLOBAL["dict"].copy()
    return pred.thread_run_prediction_single(charge_limit, charge_window, export_window, export_limits, pv10, end_record, step)


def wrapped_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv10, all_n, end_record):
    global PRED_GLOBAL
    pred = Prediction()
    pred.__dict__ = PRED_GLOBAL["dict"].copy()
    return pred.thread_run_prediction_charge(try_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv10, all_n, end_record)


def wrapped_run_prediction_charge_min_max(try_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv10, all_n, end_record):
    global PRED_GLOBAL
    pred = Prediction()
    pred.__dict__ = PRED_GLOBAL["dict"].copy()
    return pred.thread_run_prediction_charge_min_max(try_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv10, all_n, end_record)


def wrapped_run_prediction_export(this_export_limit, start, window_n, charge_limit, charge_window, export_window, export_limits, pv10, all_n, end_record):
    global PRED_GLOBAL
    pred = Prediction()
    pred.__dict__ = PRED_GLOBAL["dict"].copy()
    return pred.thread_run_prediction_export(this_export_limit, start, window_n, charge_limit, charge_window, export_window, export_limits, pv10, all_n, end_record)


def get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp):
    """
    Get AC output difference
    """
    battery_balance = battery_draw + pv_dc
    battery_balance = battery_balance * inverter_loss if battery_balance > 0 else battery_balance * inverter_loss_recp
    diff = load_yesterday - battery_balance - pv_ac
    return diff


def get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid):
    """
    Get total inverter power
    """
    battery_balance = battery_draw + pv_dc

    if battery_balance > 0:
        total_inverted = battery_balance
    else:
        total_inverted = abs(battery_balance) / inverter_loss

    if inverter_hybrid:
        total_inverted = total_inverted + pv_ac / inverter_loss

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
            self.time_abs_str = base.time_abs_str
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
            self.set_export_freeze = base.set_export_freeze
            self.set_export_freeze_only = base.set_export_freeze_only
            self.set_discharge_during_charge = base.set_discharge_during_charge
            self.set_read_only = base.set_read_only
            self.set_charge_low_power = base.set_charge_low_power
            self.set_export_low_power = base.set_export_low_power
            self.set_charge_window = base.set_charge_window
            self.set_export_window = base.set_export_window
            self.charge_low_power_margin = base.charge_low_power_margin
            self.car_charging_slots = base.car_charging_slots
            self.car_charging_limit = base.car_charging_limit
            self.car_charging_from_battery = base.car_charging_from_battery
            self.iboost_enable = base.iboost_enable
            self.iboost_on_export = base.iboost_on_export
            self.iboost_prevent_discharge = base.iboost_prevent_discharge
            self.carbon_enable = base.carbon_enable
            self.iboost_next = base.iboost_next
            self.iboost_max_energy = base.iboost_max_energy
            self.iboost_max_power = base.iboost_max_power
            self.iboost_min_power = base.iboost_min_power
            self.iboost_min_soc = base.iboost_min_soc
            self.iboost_solar = base.iboost_solar
            self.iboost_solar_excess = base.iboost_solar_excess
            self.iboost_charging = base.iboost_charging
            self.iboost_plan = base.iboost_plan
            self.iboost_gas = base.iboost_gas
            self.iboost_gas_export = base.iboost_gas_export
            self.iboost_gas_scale = base.iboost_gas_scale
            self.iboost_rate_threshold = base.iboost_rate_threshold
            self.iboost_rate_threshold_export = base.iboost_rate_threshold_export
            self.rate_gas = base.rate_gas
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
            self.battery_temperature = base.battery_temperature
            self.battery_temperature_charge_curve = base.battery_temperature_charge_curve
            self.battery_temperature_discharge_curve = base.battery_temperature_discharge_curve
            self.battery_temperature_prediction = base.battery_temperature_prediction
            self.battery_rate_max_scaling = base.battery_rate_max_scaling
            self.battery_rate_max_scaling_discharge = base.battery_rate_max_scaling_discharge
            self.battery_loss = base.battery_loss
            self.battery_loss_discharge = base.battery_loss_discharge
            self.best_soc_keep = base.best_soc_keep
            self.best_soc_keep_weight = base.best_soc_keep_weight
            self.best_soc_min = base.best_soc_min
            self.car_charging_battery_size = base.car_charging_battery_size
            self.rate_import = base.rate_import
            self.rate_export = base.rate_export
            self.pv_forecast_minute_step = pv_forecast_minute_step
            self.pv_forecast_minute10_step = pv_forecast_minute10_step
            self.load_minutes_step = load_minutes_step
            self.load_minutes_step10 = load_minutes_step10
            self.carbon_intensity = base.carbon_intensity
            self.alert_active_keep = base.alert_active_keep
            self.iboost_running = False
            self.iboost_running_solar = False
            self.iboost_running_full = False
            self.inverter_can_charge_during_export = base.inverter_can_charge_during_export
            self.prediction_cache_enable = base.prediction_cache_enable
            self.prediction_cache = {}

            # Store this dictionary in global so we can reconstruct it in the thread without passing the data
            PRED_GLOBAL["dict"] = self.__dict__.copy()

    def thread_run_prediction_single(self, charge_limit, charge_window, export_window, export_limits, pv10, end_record, step):
        """
        Run single prediction in a thread
        """

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
            predict_soc,
            car_charging_soc_next,
            iboost_next,
            iboost_running,
            iboost_running_solar,
            iboost_running_full,
        ) = self.run_prediction(charge_limit, charge_window, export_window, export_limits, pv10, end_record=end_record, step=step, cache=self.prediction_cache_enable)
        return (cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g)

    def thread_run_prediction_charge(self, try_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv10, all_n, end_record):
        """
        Run prediction in a thread
        """

        try_charge_limit = charge_limit.copy()
        if all_n:
            for set_n in all_n:
                try_charge_limit[set_n] = try_soc
        else:
            try_charge_limit[window_n] = try_soc

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
            predict_soc,
            car_charging_soc_next,
            iboost_next,
            iboost_running,
            iboost_running_solar,
            iboost_running_full,
        ) = self.run_prediction(try_charge_limit, charge_window, export_window, export_limits, pv10, end_record=end_record, cache=self.prediction_cache_enable)
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
        )

    def thread_run_prediction_charge_min_max(self, try_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv10, all_n, end_record):
        """
        Run prediction in a thread
        """

        try_charge_limit = charge_limit.copy()
        if all_n:
            for set_n in all_n:
                try_charge_limit[set_n] = try_soc
        else:
            try_charge_limit[window_n] = try_soc

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
            predict_soc,
            car_charging_soc_next,
            iboost_next,
            iboost_running,
            iboost_running_solar,
            iboost_running_full,
        ) = self.run_prediction(try_charge_limit, charge_window, export_window, export_limits, pv10, end_record=end_record, cache=False)
        min_soc = self.soc_max
        max_soc = 0
        if not all_n:
            window = charge_window[window_n]
            predict_minute_start = max(int((window["start"] - self.minutes_now) / 5) * 5, 0)
            predict_minute_end = int((window["end"] - self.minutes_now) / 5) * 5
            for minute in range(predict_minute_start, predict_minute_end + 5, 5):
                if minute in predict_soc:
                    min_soc = min(predict_soc[minute], min_soc)
                    max_soc = max(predict_soc[minute], max_soc)
            max_soc = max(max_soc, min_soc)
            min_soc = min(min_soc, max_soc)

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

    def thread_run_prediction_export(self, this_export_limit, start, window_n, charge_limit, charge_window, export_window, export_limits, pv10, all_n, end_record):
        """
        Run prediction in a thread
        """
        # Store try value into the window
        export_limits = export_limits.copy()

        if all_n:
            for window_id in all_n:
                export_limits[window_id] = this_export_limit
        else:
            export_limits[window_n] = this_export_limit
            # Adjust start
            window = export_window[window_n]
            start = min(start, window["end"] - 5)
            export_window[window_n]["start"] = start

        (
            metricmid,
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
            predict_soc,
            car_charging_soc_next,
            iboost_next,
            iboost_running,
            iboost_running_solar,
            iboost_running_full,
        ) = self.run_prediction(charge_limit, charge_window, export_window, export_limits, pv10, end_record=end_record, cache=self.prediction_cache_enable)
        return metricmid, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g

    def find_charge_window_optimised(self, charge_windows, charge_limit, is_export=False):
        """
        Takes in an array of charge windows
        Returns a dictionary defining for each minute that is in the charge window will contain the window number
        """
        charge_window_optimised = {}
        for window_n in range(len(charge_windows)):
            for minute in range(charge_windows[window_n]["start"], charge_windows[window_n]["end"], PREDICT_STEP):
                if is_export and charge_limit[window_n] < 100.0:
                    charge_window_optimised[minute] = window_n
                elif not is_export and charge_limit[window_n] > 0.0:
                    charge_window_optimised[minute] = window_n
        return charge_window_optimised

    def run_prediction(self, charge_limit, charge_window, export_window, export_limits, pv10, end_record, save=None, step=PREDICT_STEP, cache=False):
        """
        Run a prediction scenario given a charge limit, return the results
        """
        window_hash = 0
        for window in charge_window:
            window_hash ^= hash(window["start"]) ^ hash(window["end"])
        for window in export_window:
            window_hash ^= hash(window["start"]) ^ hash(window["end"])

        sim_hash = hash(tuple(charge_limit)) ^ window_hash ^ hash(tuple(export_limits)) ^ hash(pv10) ^ hash(end_record) ^ hash(step)

        if not save and cache and sim_hash in self.prediction_cache:
            # Return cached result
            return self.prediction_cache[sim_hash]

        # Fetch data from globals, optimised away from class to avoid passing it between threads
        if pv10:
            pv_forecast_minute_step = self.pv_forecast_minute10_step
            load_minutes_step = self.load_minutes_step10
        else:
            pv_forecast_minute_step = self.pv_forecast_minute_step
            load_minutes_step = self.load_minutes_step

        rate_import = self.rate_import
        rate_export = self.rate_export

        # Data structures creating during the prediction
        predict_soc = {}
        self.predict_soc_best = {}
        self.predict_metric_best = {}
        self.predict_iboost_best = {}
        self.predict_carbon_best = {}
        self.predict_clipped_best = {}
        self.iboost_running = False
        self.iboost_running_solar = False
        self.iboost_running_full = False

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
        minute_left = self.forecast_minutes
        soc = self.soc_kw
        soc_min = self.soc_max
        soc_min_minute = self.minutes_now
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
        clipped_today = 0
        predict_soc = {}
        car_charging_soc_next = self.car_charging_soc_next[:]
        iboost_next = self.iboost_next
        iboost_running = self.iboost_running
        iboost_running_solar = self.iboost_running_solar
        iboost_running_full = self.iboost_running_full

        # Remove intersecting windows and optimise the data format of the charge/discharge window
        charge_limit, charge_window = remove_intersecting_windows(charge_limit, charge_window, export_limits, export_window)
        charge_window_optimised = self.find_charge_window_optimised(charge_window, charge_limit)
        export_window_optimised = self.find_charge_window_optimised(export_window, export_limits, is_export=True)

        # For the SOC calculation we need to stop 24 hours after the first charging window starts
        # to avoid wrapping into the next day
        record = True

        # Battery behaviour
        if self.inverter_hybrid:
            inverter_loss_ac = self.inverter_loss
        else:
            inverter_loss_ac = 1.0
        inverter_loss = self.inverter_loss
        inverter_hybrid = self.inverter_hybrid
        inverter_loss_recp = 1 / inverter_loss

        enable_standing_charge = save and (save in ["best", "base", "base10", "best10", "test"])
        enable_save_stats = save and (save in ["best", "test", "compare"])
        car_enable = self.num_cars > 0
        inverter_limit = self.inverter_limit * step
        export_limit = self.export_limit * step
        set_charge_low_power = self.set_charge_window and self.set_charge_low_power and (save in ["best", "best10", "test"])
        carbon_enable = self.carbon_enable
        reserve = self.reserve
        soc_max = self.soc_max
        battery_loss = self.battery_loss
        battery_loss_discharge = self.battery_loss_discharge
        battery_temperature_prediction = self.battery_temperature_prediction
        alert_active_keep = self.alert_active_keep
        best_soc_keep_weight = self.best_soc_keep_weight
        best_soc_keep_orig = self.best_soc_keep
        debug_enable = self.debug_enable
        set_reserve_enable = self.set_reserve_enable
        set_export_freeze = self.set_export_freeze
        set_export_freeze_only = self.set_export_freeze_only
        set_charge_window = self.set_charge_window
        set_export_window = self.set_export_window
        battery_rate_max_charge = self.battery_rate_max_charge
        battery_rate_max_discharge = self.battery_rate_max_discharge
        battery_temperature_charge_curve = self.battery_temperature_charge_curve
        battery_rate_min = self.battery_rate_min
        carbon_intensity = self.carbon_intensity
        set_discharge_during_charge = self.set_discharge_during_charge

        # Get PV step for the current step itself
        pv_forecast_minute_step_flat = {}
        load_minutes_step_flat = {}

        if step != PREDICT_STEP:
            for minute in range(0, self.forecast_minutes, step):
                pv_now = 0
                load_yesterday = 0
                for offset in range(0, step, PREDICT_STEP):
                    pv_now += pv_forecast_minute_step[minute + offset]
                    load_yesterday += load_minutes_step[minute + offset]
                pv_forecast_minute_step_flat[minute] = pv_now
                load_minutes_step_flat[minute] = load_yesterday
        else:
            pv_forecast_minute_step_flat = pv_forecast_minute_step
            load_minutes_step_flat = load_minutes_step

        # Simulate each forward minute
        minute = 0
        while minute < self.forecast_minutes:
            # Minute yesterday can wrap if days_previous is only 1
            minute_absolute = minute + self.minutes_now
            prev_soc = soc
            reserve_expected = reserve
            import_rate = rate_import.get(minute_absolute, 0)
            export_rate = rate_export.get(minute_absolute, 0)

            # Alert?
            alert_keep = alert_active_keep.get(minute_absolute, 0)

            # Project battery temperature
            battery_temperature = battery_temperature_prediction.get(minute, self.battery_temperature)

            # Once a force discharge is set the four hour rule is disabled
            if four_hour_rule:
                keep_minute_scaling = min((minute / 240), 1.0) * best_soc_keep_weight
            else:
                keep_minute_scaling = best_soc_keep_weight

            # Get soc keep value
            best_soc_keep = best_soc_keep_orig

            # Alert keep - force scaling to 1 and set new keep value
            if alert_keep > 0:
                keep_minute_scaling = max(keep_minute_scaling, 2.0)
                best_soc_keep = max(best_soc_keep, min(alert_keep / 100.0 * soc_max, soc_max))

            # Find charge & discharge windows
            charge_window_n = charge_window_optimised.get(minute_absolute, -1)
            export_window_n = export_window_optimised.get(minute_absolute, -1)
            charge_window_active = charge_window_n >= 0
            export_window_active = export_window_n >= 0
            export_limit_now = export_limits[export_window_n] if export_window_active else 100.0

            # Find charge limit
            charge_limit_n = 0
            if charge_window_active:
                charge_limit_n = charge_limit[charge_window_n]
                if self.set_charge_freeze and (charge_limit_n == reserve):
                    # Charge freeze via reserve
                    charge_limit_n = max(soc, reserve)

                # When set reserve enable is on pretend the reserve is the charge limit minus the
                # minimum battery rate modelled as it can leak a little
                if set_reserve_enable and (soc >= charge_limit_n):
                    reserve_expected = max(charge_limit_n, reserve)

            # Outside the recording window?
            if record and minute >= end_record:
                record = False

            # Save Soc prediction data as minutes for later use
            if not cache or debug_enable or save:
                predict_soc[minute] = round(soc, 3)

            # Store data before the next simulation step to align timestamps
            if debug_enable or save:
                minute_timestamp = self.midnight_utc + timedelta(seconds=60 * minute_absolute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                predict_soc_time[stamp] = round(soc, 3)
                metric_time[stamp] = round(metric, 3)
                load_kwh_time[stamp] = round(load_kwh, 3)
                pv_kwh_time[stamp] = round(pv_kwh, 2)
                import_kwh_time[stamp] = round(import_kwh, 2)
                export_kwh_time[stamp] = round(export_kwh, 2)
                for car_n in range(self.num_cars):
                    predict_car_soc_time[car_n][stamp] = round(car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0, 2)
                predict_iboost[stamp] = iboost_today_kwh
                record_time[stamp] = 0 if record else soc_max
                if enable_save_stats:
                    self.predict_soc_best[minute] = round(soc, 3)
                    self.predict_metric_best[minute] = round(metric, 3)
                    self.predict_iboost_best[minute] = round(iboost_today_kwh, 2)
                    self.predict_carbon_best[minute] = round(carbon_g, 0)
                    self.predict_clipped_best[minute] = round(clipped_today, 2)
            else:
                stamp = ""

            # Add in standing charge, only for the final plan when we save the results
            if enable_standing_charge and (minute_absolute % (24 * 60)) < step:
                metric += self.metric_standing_charge

            # Get load and pv forecast, total up for all values in the step
            pv_now = pv_forecast_minute_step_flat[minute]
            load_yesterday = load_minutes_step_flat[minute]
            # for offset in range(0, step, PREDICT_STEP):
            #    pv_now += pv_forecast_minute_step[minute + offset]
            #    load_yesterday += load_minutes_step[minute + offset]

            # Count PV kWh
            pv_kwh += pv_now

            # Modelling reset of charge/discharge rate
            if set_charge_window or set_export_window:
                charge_rate_now = battery_rate_max_charge
                discharge_rate_now = battery_rate_max_discharge

            # Simulate car charging
            if car_enable:
                car_load = in_car_slot(minute_absolute, self.num_cars, self.car_charging_slots)

                # Car charging?
                for car_n in range(self.num_cars):
                    if car_load[car_n] > 0.0:
                        car_load_scale = car_load[car_n] * step / 60.0
                        car_load_scale = car_load_scale * self.car_charging_loss
                        car_load_scale = max(min(car_load_scale, self.car_charging_limit[car_n] - car_soc[car_n]), 0)
                        car_soc[car_n] = car_soc[car_n] + car_load_scale
                        load_yesterday += car_load_scale / self.car_charging_loss
                        # Model not allowing the car to charge from the battery
                        if (car_load_scale > 0) and (not self.car_charging_from_battery) and set_charge_window:
                            discharge_rate_now = battery_rate_min  # 0

            # Iboost
            iboost_rate_okay = True
            iboost_amount = 0
            iboost_freeze = False

            # IBoost energy rate control
            if self.iboost_enable:
                # Boost on energy rates
                if import_rate > self.iboost_rate_threshold:
                    iboost_rate_okay = False
                if export_rate > self.iboost_rate_threshold_export:
                    iboost_rate_okay = False

                # Boost on gas vs import rate
                if self.iboost_gas and self.rate_gas:
                    gas_rate = self.rate_gas.get(minute_absolute, 99) * self.iboost_gas_scale
                    if import_rate > gas_rate:
                        iboost_rate_okay = False

                # Boost on gas vs export rate
                if self.iboost_gas_export and self.rate_gas:
                    gas_rate = self.rate_gas.get(minute_absolute, 99) * self.iboost_gas_scale
                    if export_rate > gas_rate:
                        iboost_rate_okay = False

                # IBoost solar diverter on load, don't do on discharge
                # IBoost based on plan for given rates
                if self.iboost_plan and (self.iboost_on_export or (export_window_n < 0)):
                    iboost_load = in_iboost_slot(minute_absolute, self.iboost_plan) * step / 60.0
                    iboost_amount = min(iboost_load, self.iboost_max_power * step, max(self.iboost_max_energy - iboost_today_kwh, 0))

                # IBoost based on Predbat charging
                if self.iboost_charging and iboost_rate_okay and iboost_today_kwh < self.iboost_max_energy:
                    if charge_window_active:
                        iboost_amount = min(self.iboost_max_power * step, max(self.iboost_max_energy - iboost_today_kwh, 0))

                # Freeze discharge on iboost
                if iboost_amount > 0 and self.iboost_prevent_discharge and set_charge_window:
                    iboost_freeze = True
                    discharge_rate_now = battery_rate_min  # 0

                # Iboost running
                if iboost_amount > 0 and minute == 0:
                    iboost_running_full = True

                # Iboost load added
                load_yesterday += iboost_amount

                # iBoost Solar diversion model
                if self.iboost_solar and not self.iboost_solar_excess:
                    if iboost_rate_okay and iboost_today_kwh < self.iboost_max_energy and (pv_now > (self.iboost_min_power * step) and ((soc * 100.0 / soc_max) >= self.iboost_min_soc)) and (self.iboost_on_export or (export_window_n < 0)):
                        iboost_pv_amount = min(pv_now, max(self.iboost_max_power * step - iboost_amount, 0), max(self.iboost_max_energy - iboost_today_kwh - iboost_amount, 0))
                        pv_now -= iboost_pv_amount
                        iboost_amount += iboost_pv_amount
                        if iboost_pv_amount > 0 and minute == 0:
                            iboost_running_solar = True

            # Count load
            load_kwh += load_yesterday

            # discharge freeze, reset charge rate by default
            if set_export_freeze:
                # Freeze mode
                if (export_window_active) and export_limit_now < 100.0 and (set_export_freeze and (export_limit_now == 99.0 or set_export_freeze_only)):
                    charge_rate_now = battery_rate_min  # 0

            # Set discharge during charge?
            if charge_window_active:
                if not set_discharge_during_charge:
                    discharge_rate_now = battery_rate_min
                elif set_charge_window and soc >= charge_limit_n and (abs(calc_percent_limit(soc, soc_max) - calc_percent_limit(charge_limit_n, soc_max)) <= 1.0):
                    discharge_rate_now = battery_rate_min

            # Current real charge rate
            charge_rate_now_curve = get_charge_rate_curve(soc, charge_rate_now, soc_max, battery_rate_max_charge, self.battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_charge_curve) * self.battery_rate_max_scaling
            charge_rate_now_curve_step = charge_rate_now_curve * step
            discharge_rate_now_curve = (
                get_discharge_rate_curve(soc, discharge_rate_now, soc_max, battery_rate_max_discharge, self.battery_discharge_power_curve, battery_rate_min, battery_temperature, self.battery_temperature_discharge_curve)
                * self.battery_rate_max_scaling_discharge
            )
            discharge_rate_now_curve_step = discharge_rate_now_curve * step

            battery_to_min = max(soc - reserve_expected, 0) * battery_loss_discharge
            battery_to_max = max(soc_max - soc, 0) * battery_loss

            discharge_min = reserve
            if export_window_active:
                discharge_min = max(soc_max * export_limit_now / 100.0, reserve, self.best_soc_min)

            if not set_export_freeze_only and export_window_active and export_limit_now < 99.0 and (soc > discharge_min):
                # Discharge enable
                discharge_rate_now = battery_rate_max_discharge  # Assume discharge becomes enabled here
                if self.set_export_low_power:
                    export_rate_adjust = 1 - (export_limit_now - int(export_limit_now))
                else:
                    export_rate_adjust = 1.0
                discharge_rate_now = battery_rate_max_discharge * export_rate_adjust
                discharge_rate_now_curve = (
                    get_discharge_rate_curve(soc, discharge_rate_now, soc_max, battery_rate_max_discharge, self.battery_discharge_power_curve, battery_rate_min, battery_temperature, self.battery_temperature_discharge_curve)
                    * self.battery_rate_max_scaling_discharge
                )
                discharge_rate_now_curve_step = discharge_rate_now_curve * step

                battery_draw = min(discharge_rate_now_curve_step, battery_to_min)

                pv_ac = pv_now * inverter_loss_ac
                pv_dc = 0

                # Exceed export limit?
                diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp)
                if diff < 0 and abs(diff) > export_limit:
                    over_limit = abs(diff) - export_limit
                    reduce_by = over_limit

                    if reduce_by > battery_draw:
                        if self.inverter_can_charge_during_export:
                            reduce_by = reduce_by - battery_draw
                            battery_draw = max(-reduce_by * inverter_loss, -battery_to_min, -charge_rate_now_curve_step)
                        else:
                            battery_draw = 0
                    else:
                        battery_draw = battery_draw - reduce_by

                    if inverter_hybrid and battery_draw < 0:
                        pv_dc = min(abs(battery_draw), pv_now)
                        pv_ac = (pv_now - pv_dc) * inverter_loss_ac

                # Exceeds inverter limit, scale back discharge?
                total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid)
                if inverter_hybrid:
                    over_limit = total_inverted - inverter_limit
                    if total_inverted > inverter_limit:
                        reduce_by = over_limit
                        if reduce_by > battery_draw:
                            reduce_by = reduce_by - battery_draw
                            battery_draw = 0
                            if self.inverter_can_charge_during_export:
                                battery_draw = max(-reduce_by * inverter_loss, -battery_to_min, -charge_rate_now_curve_step)
                        else:
                            battery_draw = battery_draw - reduce_by

                        if battery_draw < 0:
                            pv_dc = min(abs(battery_draw), pv_now)
                        pv_ac = (pv_now - pv_dc) * inverter_loss_ac
                else:
                    if total_inverted > inverter_limit:
                        over_limit = total_inverted - inverter_limit
                        battery_draw = max(battery_draw - over_limit * inverter_loss, 0)

                if battery_draw < 0:
                    battery_state = "f/"
                else:
                    battery_state = "f-"

                # Once force discharge starts the four hour rule is disabled
                four_hour_rule = False
            elif charge_window_active and soc < charge_limit_n:
                # Charge enable
                # Only tune charge rate on final plan not every simulation
                charge_rate_now, charge_rate_now_curve = find_charge_rate(
                    minute_absolute,
                    soc,
                    charge_window[charge_window_n],
                    charge_limit_n,
                    battery_rate_max_charge,
                    soc_max,
                    self.battery_charge_power_curve,
                    set_charge_low_power,
                    self.charge_low_power_margin,
                    battery_rate_min,
                    self.battery_rate_max_scaling,
                    battery_loss,
                    None,
                    battery_temperature,
                    battery_temperature_charge_curve,
                )
                charge_rate_now_curve_step = charge_rate_now_curve * step

                battery_draw = -max(min(charge_rate_now_curve_step, max(charge_limit_n - soc, pv_now)), 0, -battery_to_max)
                battery_state = "f+"
                first_charge = min(first_charge, minute)

                if inverter_hybrid:
                    pv_dc = min(abs(battery_draw), pv_now)
                else:
                    pv_dc = 0
                pv_ac = (pv_now - pv_dc) * inverter_loss_ac

                if (charge_limit_n - soc) < (charge_rate_now_curve_step):
                    # The battery will hit the charge limit in this period, so if the charge was spread over the period
                    # it could be done from solar, but in reality it will be full rate and then stop meaning the solar
                    # won't cover it and it will likely create an import.
                    pv_compare = pv_dc + pv_ac
                    if pv_dc >= (charge_limit_n - soc) and (pv_compare < (charge_rate_now_curve_step)):
                        charge_time_remains = (charge_limit_n - soc) / charge_rate_now_curve  # Time in minute periods left
                        pv_in_period = pv_compare / step * charge_time_remains
                        potential_import = min((charge_rate_now_curve * charge_time_remains) - pv_in_period, (charge_limit_n - soc))
                        metric_keep += max(potential_import * import_rate, 0)
            else:
                # ECO Mode
                pv_ac = pv_now * inverter_loss_ac
                pv_dc = 0
                diff = get_diff(0, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp)

                required_for_load = load_yesterday * inverter_loss_recp
                if inverter_hybrid:
                    potential_to_charge = pv_now
                else:
                    potential_to_charge = pv_ac

                diff = required_for_load - potential_to_charge

                if diff > 0:
                    battery_draw = min(diff, discharge_rate_now_curve_step, inverter_limit, battery_to_min)
                    battery_state = "e-"
                else:
                    battery_draw = max(diff, -charge_rate_now_curve_step, -inverter_limit, -battery_to_max)
                    if battery_draw < 0:
                        battery_state = "e+"
                    else:
                        battery_state = "e~"

                    if inverter_hybrid:
                        pv_dc = min(abs(battery_draw), pv_now)
                    else:
                        pv_dc = 0
                    pv_ac = (pv_now - pv_dc) * inverter_loss_ac

            # Clamp at inverter limit
            if inverter_hybrid:
                battery_inverted = get_total_inverted(battery_draw, pv_dc, 0, inverter_loss, inverter_hybrid)
                if battery_inverted > inverter_limit:
                    over_limit = battery_inverted - inverter_limit

                    if battery_draw + pv_dc > 0:
                        battery_draw = max(battery_draw - over_limit, 0)
                    else:
                        battery_draw = min(battery_draw + over_limit * inverter_loss, 0)

                    # Adjustment to charging from solar case
                    if battery_draw < 0:
                        pv_dc = min(abs(battery_draw), pv_now)
                        pv_ac = (pv_now - pv_dc) * inverter_loss_ac

                # Clip battery discharge back
                total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid)
                if total_inverted > inverter_limit and (battery_draw + pv_dc) > 0:
                    over_limit = total_inverted - inverter_limit
                    if battery_draw + pv_dc > 0:
                        battery_draw = max(battery_draw - over_limit, 0)

                    if battery_draw == 0:
                        total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid)
                        over_limit = 0
                        if total_inverted > inverter_limit:
                            over_limit = total_inverted - inverter_limit
                        battery_draw = max(-over_limit * inverter_loss, -charge_rate_now_curve_step, -battery_to_max, -pv_ac)

                    if battery_draw < 0:
                        pv_dc = min(abs(battery_draw), pv_now)
                        pv_ac = (pv_now - pv_dc) * inverter_loss_ac

                # Clip solar
                total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid)
                if total_inverted > inverter_limit:
                    over_limit = total_inverted - inverter_limit
                    clipped_today += over_limit
                    pv_ac = max(pv_ac - over_limit * inverter_loss, 0)
            else:
                total_inverted = get_total_inverted(battery_draw, pv_dc, pv_ac, inverter_loss, inverter_hybrid)
                if total_inverted > inverter_limit:
                    over_limit = total_inverted - inverter_limit
                    if battery_draw > 0:
                        battery_draw = max(battery_draw - over_limit, 0)
                    else:
                        battery_draw = min(battery_draw + over_limit * inverter_loss, 0)

            # Export limit, clip PV output
            diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp)
            if diff < 0 and abs(diff) > export_limit:
                over_limit = abs(diff) - export_limit
                clipped_today += over_limit
                pv_ac = max(pv_ac - over_limit, 0)

            # Adjust battery soc
            if battery_draw > 0:
                soc = max(soc - battery_draw / battery_loss_discharge, reserve_expected)
            else:
                soc = min(soc - battery_draw * battery_loss, soc_max)
            soc = round(soc, 6)

            # Iboost finally count
            if self.iboost_enable:
                # iBoost Solar diversion model
                if self.iboost_solar and self.iboost_solar_excess:
                    excess = 0
                    if diff < 0:
                        excess = -diff
                    if iboost_rate_okay and iboost_today_kwh < self.iboost_max_energy and (excess > (self.iboost_min_power * step) and ((soc * 100.0 / soc_max) >= self.iboost_min_soc)) and (self.iboost_on_export or (export_window_n < 0)):
                        iboost_pv_amount = min(excess, max(self.iboost_max_power * step - iboost_amount, 0), max(self.iboost_max_energy - iboost_today_kwh - iboost_amount, 0))
                        load_yesterday += iboost_pv_amount
                        iboost_amount += iboost_pv_amount
                        if iboost_pv_amount > 0 and minute == 0:
                            iboost_running_solar = True

                # Cumulative iBoost energy
                iboost_today_kwh += iboost_amount

                # Model iboost reset
                if (minute_absolute % (24 * 60)) == ((24 * 60) - step):
                    iboost_today_kwh = 0

                # Save iBoost next prediction
                if minute == 0:
                    scaled_boost = (iboost_amount / step) * RUN_EVERY
                    iboost_next = round((self.iboost_today + scaled_boost), 6)
                    if iboost_next > self.iboost_today:
                        iboost_running = True

            # Count battery cycles
            battery_cycle = battery_cycle + abs(battery_draw)

            # Work out left over energy after battery adjustment
            diff = get_diff(battery_draw, pv_dc, pv_ac, load_yesterday, inverter_loss, inverter_loss_recp)

            # Metric keep - pretend the battery is empty and you have to import instead of using the battery
            if best_soc_keep > 0 and soc <= best_soc_keep:
                metric_keep += (best_soc_keep - soc) * import_rate * keep_minute_scaling * step / 60.0

            if diff > 0:
                # Import
                # All imports must go to home (no inverter loss) or to the battery (inverter loss accounted before above)
                import_kwh += diff

                if carbon_enable:
                    carbon_g += diff * carbon_intensity.get(minute, 0)

                if charge_window_active:
                    # If the battery is on charge anyhow then imports are at battery charging rate
                    import_kwh_battery += diff
                else:
                    # self.log("importing to minute %s amount %s kW total %s kWh total draw %s" % (minute, energy, import_kwh_house, diff))
                    import_kwh_house += diff

                metric += import_rate * diff
                grid_state = "<"
            else:
                # Export
                energy = -diff
                export_kwh += energy
                if carbon_enable:
                    carbon_g -= energy * carbon_intensity.get(minute, 0)

                metric -= export_rate * energy
                if diff != 0:
                    grid_state = ">"
                else:
                    grid_state = "~"

            # Record final soc & metric
            if record:
                # Store the number of minutes until the battery runs out
                if soc <= reserve:
                    minute_left = min(minute, minute_left)
                final_soc = soc

                if car_enable:
                    for car_n in range(self.num_cars):
                        final_car_soc[car_n] = round(car_soc[car_n], 3)
                        if minute == 0:
                            # Next car SOC
                            car_charging_soc_next[car_n] = round(car_soc[car_n], 3)

                final_metric = metric
                final_import_kwh = import_kwh
                final_import_kwh_battery = import_kwh_battery
                final_import_kwh_house = import_kwh_house
                final_export_kwh = export_kwh
                final_iboost_kwh += iboost_amount
                final_battery_cycle = battery_cycle
                final_metric_keep = metric_keep
                final_carbon_g = carbon_g
                final_load_kwh = load_kwh
                final_pv_kwh = pv_kwh

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

                # Record soc min
                if soc < soc_min:
                    soc_min_minute = minute_absolute
                soc_min = min(soc_min, soc)

            # Record state
            if debug_enable or save:
                predict_state[stamp] = "g" + grid_state + "b" + battery_state
                predict_battery_power[stamp] = round(battery_draw * (60 / step), 3)
                predict_battery_cycle[stamp] = round(battery_cycle, 3)
                predict_pv_power[stamp] = round((pv_forecast_minute_step[minute] + pv_forecast_minute_step.get(minute + step, 0)) * (30 / step), 3)
                predict_grid_power[stamp] = round(diff * (60 / step), 3)
                predict_load_power[stamp] = round(load_yesterday * (60 / step), 3)
                if carbon_enable:
                    predict_carbon_g[stamp] = round(carbon_g, 3)

            minute += step

        hours_left = minute_left / 60.0
        if self.debug_enable or save:
            self.hours_left = hours_left
            self.final_car_soc = final_car_soc
            self.predict_car_soc_time = predict_car_soc_time
            self.final_soc = round(final_soc, 4)
            self.final_metric = round(final_metric, 4)
            self.final_metric_keep = round(final_metric_keep, 4)
            self.final_import_kwh = round(final_import_kwh, 4)
            self.final_import_kwh_battery = round(final_import_kwh_battery, 4)
            self.final_import_kwh_house = round(final_import_kwh_house, 4)
            self.final_export_kwh = round(final_export_kwh, 4)
            self.final_load_kwh = round(final_load_kwh, 4)
            self.final_pv_kwh = round(final_pv_kwh, 4)
            self.final_iboost_kwh = round(final_iboost_kwh, 4)
            self.final_battery_cycle = round(final_battery_cycle, 4)
            self.final_soc_min = round(soc_min, 4)
            self.final_soc_min_minute = soc_min_minute
            self.export_to_first_charge = export_to_first_charge
            self.predict_soc_time = predict_soc_time
            self.first_charge = first_charge
            self.first_charge_soc = round(first_charge_soc, 4)
            self.predict_state = predict_state
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
            self.predict_soc = predict_soc
            self.pv_kwh_h0 = round(pv_kwh_h0, 4)
            self.import_kwh_h0 = round(import_kwh_h0, 4)
            self.export_kwh_h0 = round(export_kwh_h0, 4)
            self.load_kwh_h0 = round(load_kwh_h0, 4)
            self.load_kwh_time = load_kwh_time
            self.pv_kwh_time = pv_kwh_time
            self.import_kwh_time = import_kwh_time
            self.export_kwh_time = export_kwh_time

        if not save and cache:
            self.prediction_cache[sim_hash] = (
                round(final_metric, 4),
                round(import_kwh_battery, 4),
                round(import_kwh_house, 4),
                round(export_kwh, 4),
                round(soc_min, 4),
                round(final_soc, 4),
                soc_min_minute,
                round(final_battery_cycle, 4),
                round(final_metric_keep, 4),
                round(final_iboost_kwh, 4),
                round(final_carbon_g, 4),
                [],
                [],
                iboost_next,
                iboost_running,
                iboost_running_solar,
                iboost_running_full,
            )

        return (
            round(final_metric, 4),
            round(import_kwh_battery, 4),
            round(import_kwh_house, 4),
            round(export_kwh, 4),
            round(soc_min, 4),
            round(final_soc, 4),
            soc_min_minute,
            round(final_battery_cycle, 4),
            round(final_metric_keep, 4),
            round(final_iboost_kwh, 4),
            round(final_carbon_g, 4),
            predict_soc,
            car_charging_soc_next,
            iboost_next,
            iboost_running,
            iboost_running_solar,
            iboost_running_full,
        )
