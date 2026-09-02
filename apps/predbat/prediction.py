# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Minute-by-minute battery simulation engine.

Implements the core prediction model that simulates energy flows (PV generation,
load consumption, battery charge/discharge, grid import/export) for each minute
of the forecast period. Used by the optimiser to evaluate different charge/discharge
plans and select the one with the lowest cost metric.
"""

from datetime import timedelta
from const import PREDICT_STEP, PV_SCENARIO_PV10, PV_SCENARIO_PV90, RUN_EVERY, TIME_FORMAT, EXPORT_LIMIT_FREEZE, EXPORT_LIMIT_IDLE

from utils import remove_intersecting_windows, get_charge_rate_curve_cached, get_discharge_rate_curve_cached, find_charge_rate, calc_percent_limit, in_iboost_slot, in_car_slot, charge_curve_to_tuple
from prediction_batch import PredictionBatch, prediction_cache_key
from prediction_kernel import create_kernel_context, kernel_supported, run_prediction_kernel


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


class Prediction(PredictionBatch):
    """
    Class to hold prediction input and output data and the run function
    """

    def __init__(
        self,
        base=None,
        pv_forecast_minute_step=None,
        pv_forecast_minute10_step=None,
        load_minutes_step=None,
        load_minutes_step10=None,
        pv_forecast_minute90_step=None,
        load_minutes_step90=None,
        soc_kw=None,
        soc_max=None,
        kernel_static_cache=None,
        clipping_limit=0,
        clipping_cost_weight=0,
        clipping_buffer_kwh=0,
        clipping_buffer_start=None,
        clipping_buffer_end=None,
    ):
        """Build a Prediction, optionally copying simulation state from a base PredBat instance.

        pv_forecast_minute90_step and load_minutes_step90 fall back to the nominal step arrays when None, so
        every existing call site that never requests the pv90 scenario keeps working unchanged.

        kernel_static_cache is passed straight through to create_kernel_context, for a caller building
        several Predictions that differ only in their load forecast; see that function for the contract.
        """
        if base:
            self.minutes_now = base.minutes_now
            self.log = base.log
            self.time_abs_str = base.time_abs_str
            self.forecast_minutes = base.forecast_minutes
            self.midnight_utc = base.midnight_utc
            self.soc_kw = soc_kw if soc_kw is not None else base.soc_kw
            self.soc_max = soc_max if soc_max is not None else base.soc_max
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
            self.car_energy_reported_load = base.car_energy_reported_load
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
            self.calculate_export_on_pv = base.calculate_export_on_pv
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
            self.inverter_freeze_export_discharge_rate = base.inverter_freeze_export_discharge_rate
            self.inverter_hybrid = base.inverter_hybrid
            self.inverter_limit = base.inverter_limit
            self.export_limit = base.export_limit
            self.pv_ac_limit = base.pv_ac_limit
            self.battery_rate_min = base.battery_rate_min
            self.battery_rate_max_charge = base.battery_rate_max_charge
            self.battery_rate_max_charge_dc = base.battery_rate_max_charge_dc
            self.battery_rate_max_discharge = base.battery_rate_max_discharge
            self.battery_rate_max_export = base.battery_rate_max_export
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
            self.io_adjusted = base.io_adjusted
            self.rate_max = base.rate_max
            self.clipping_buffer_enable = getattr(base, "clipping_buffer_enable", False)
            self.pv_forecast_minute_step = pv_forecast_minute_step
            self.pv_forecast_minute10_step = pv_forecast_minute10_step
            self.load_minutes_step = load_minutes_step
            self.load_minutes_step10 = load_minutes_step10
            self.pv_forecast_minute90_step = pv_forecast_minute90_step if pv_forecast_minute90_step is not None else pv_forecast_minute_step
            self.load_minutes_step90 = load_minutes_step90 if load_minutes_step90 is not None else load_minutes_step
            self.carbon_intensity = base.carbon_intensity
            self.all_active_keep = base.all_active_keep
            self.iboost_running = False
            self.iboost_running_solar = False
            self.iboost_running_full = False
            self.inverter_can_charge_during_export = base.inverter_can_charge_during_export
            self.inverter_support_feedin_first = base.inverter_support_feedin_first
            self.prediction_cache_enable = base.prediction_cache_enable
            self.prediction_cache = {}
            self.plan_interval_minutes = base.plan_interval_minutes
            self.charge_scaling10 = base.charge_scaling10

            # C++ prediction kernel context (0 = kernel unavailable, Python engine is used)
            self.prediction_kernel_enable = getattr(base, "prediction_kernel_enable", False)
            self.kernel_handle = 0
            if self.prediction_kernel_enable:
                self.kernel_handle = create_kernel_context(self, static_cache=kernel_static_cache)

        # Outside the `if base:` block on purpose: a Prediction built without a base is still a valid
        # object and its first enqueue_prediction would otherwise raise AttributeError
        self.pending_batch = []
        self.batch_threads = 1

    def _prepare_single(self, charge_limit, export_limits):
        """Copy the caller's limit lists for a single-scenario trial - shared by thread_run_prediction_single and the batch path.

        The copy used to live in Plan.launch_run_prediction_single. It is kept here because the batch
        path does not read these lists until the batch is flushed, so a trial has to own the copy it
        will eventually be simulated from rather than share the caller's list.
        """
        return list(charge_limit), list(export_limits)

    def _prepare_charge(self, try_soc, window_n, charge_limit, all_n):
        """Build the trial charge limits - shared by thread_run_prediction_charge/_charge_min_max and the batch path"""
        try_charge_limit = charge_limit.copy()
        if all_n:
            for set_n in all_n:
                try_charge_limit[set_n] = try_soc
        else:
            try_charge_limit[window_n] = try_soc
        return try_charge_limit

    def thread_run_prediction_single(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step):
        """Run one single-scenario prediction now and return its result.

        Nothing runs in a Python thread any more: this is the direct synchronous path, kept as the
        reference the batch is checked against and as what a queued job falls back to when the kernel
        will not take it.
        """
        charge_limit, export_limits = self._prepare_single(charge_limit, export_limits)

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
        ) = self.run_prediction(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record=end_record, step=step, cache=self.prediction_cache_enable)
        return (cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g)

    def queue_run_prediction_single(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step):
        """Queue a single-scenario prediction, returning a handle - the batch runs on the first get().

        The window lists and dicts are read at flush time, not now, so the caller must not mutate
        anything it passed in before calling get() on the returned handle.
        """
        charge_limit, export_limits = self._prepare_single(charge_limit, export_limits)
        return self.enqueue_prediction(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, self.prediction_cache_enable)

    def thread_run_prediction_charge(self, try_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Run one charge-window trial prediction now and return its result.

        Nothing runs in a Python thread any more: this is the direct synchronous path, kept as the
        reference the batch is checked against and as what a queued job falls back to when the kernel
        will not take it.
        """

        try_charge_limit = self._prepare_charge(try_soc, window_n, charge_limit, all_n)

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
        ) = self.run_prediction(try_charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record=end_record, cache=self.prediction_cache_enable)
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

    def queue_run_prediction_charge(self, try_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Queue a charge-window trial prediction, returning a handle.

        The window lists and dicts are read at flush time, not now, so the caller must not mutate
        anything it passed in before calling get() on the returned handle.
        """
        try_charge_limit = self._prepare_charge(try_soc, window_n, charge_limit, all_n)
        return self.enqueue_prediction(try_charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, PREDICT_STEP, self.prediction_cache_enable)

    def scan_soc_range(self, predict_soc, window):
        """Return the (min, max) SoC across a charge window - shared by the direct and batch min/max paths.

        The kernel computes the same range inline (see PkBatchJob.soc_range_start_step), so this is
        only reached when a job falls back to the Python engine; the two must agree exactly, including
        the clamping that collapses an empty range to a single value rather than leaving min above max.
        """
        min_soc = self.soc_max
        max_soc = 0
        predict_minute_start = max(int((window["start"] - self.minutes_now) / 5) * 5, 0)
        predict_minute_end = int((window["end"] - self.minutes_now) / 5) * 5
        for minute in range(predict_minute_start, predict_minute_end + 5, 5):
            if minute in predict_soc:
                min_soc = min(predict_soc[minute], min_soc)
                max_soc = max(predict_soc[minute], max_soc)
        max_soc = max(max_soc, min_soc)
        min_soc = min(min_soc, max_soc)
        return min_soc, max_soc

    def thread_run_prediction_charge_min_max(self, try_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Run one charge-window trial prediction now and return its result plus the SoC range.

        Nothing runs in a Python thread any more: this is the direct synchronous path, kept as the
        reference the batch is checked against and as what a queued job falls back to when the kernel
        will not take it.
        """

        try_charge_limit = self._prepare_charge(try_soc, window_n, charge_limit, all_n)

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
        ) = self.run_prediction(try_charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record=end_record, cache=False)
        min_soc = self.soc_max
        max_soc = 0
        if not all_n:
            min_soc, max_soc = self.scan_soc_range(predict_soc, charge_window[window_n])

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

    def queue_run_prediction_charge_min_max(self, try_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Queue a charge-window trial prediction that also reports the SoC range across that window.

        Uncached, exactly as the direct path is: the SoC range is not part of the cached result, so a
        hit would answer with the wrong shape.

        The window lists and dicts are read at flush time, not now, so the caller must not mutate
        anything it passed in before calling get() on the returned handle.
        """
        try_charge_limit = self._prepare_charge(try_soc, window_n, charge_limit, all_n)
        range_window = None if all_n else charge_window[window_n]
        return self.enqueue_prediction(try_charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, PREDICT_STEP, False, want_range=True, range_window=range_window)

    def _prepare_export(self, this_export_limit, start, window_n, export_window, export_limits, all_n):
        """Build the trial export limits and window list - shared by thread_run_prediction_export and the batch path.

        The trial start is applied to a private copy of the window rather than written into the
        caller's list: with a process pool each worker mutated its own unpickled copy, but a batched
        fan-out shares one list across every job in the batch, so an in-place write would corrupt the
        other trials of the same window. Only ["end"] is ever read back by the caller
        (optimise_export), so nothing depends on the write being visible.
        """
        export_limits = export_limits.copy()

        if all_n:
            for window_id in all_n:
                export_limits[window_id] = this_export_limit
        else:
            export_limits[window_n] = this_export_limit
            # Adjust start
            window = export_window[window_n]
            start = min(start, window["end"] - 5)
            export_window = list(export_window)
            export_window[window_n] = dict(window, start=start)

        return export_window, export_limits

    def thread_run_prediction_export(self, this_export_limit, start, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Run one export-window trial prediction now and return its result.

        Nothing runs in a Python thread any more: this is the direct synchronous path, kept as the
        reference the batch is checked against and as what a queued job falls back to when the kernel
        will not take it.
        """
        export_window, export_limits = self._prepare_export(this_export_limit, start, window_n, export_window, export_limits, all_n)

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
        ) = self.run_prediction(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record=end_record, cache=self.prediction_cache_enable)
        return metricmid, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g

    def queue_run_prediction_export(self, this_export_limit, start, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Queue an export-window trial prediction, returning a handle.

        The window lists and dicts are read at flush time, not now, so the caller must not mutate
        anything it passed in before calling get() on the returned handle.
        """
        export_window, export_limits = self._prepare_export(this_export_limit, start, window_n, export_window, export_limits, all_n)
        return self.enqueue_prediction(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, PREDICT_STEP, self.prediction_cache_enable)

    def find_charge_window_optimised(self, charge_windows, charge_limit, is_export=False):
        """
        Takes in an array of charge windows
        Returns a dictionary defining for each minute that is in the charge window will contain the window number
        """
        charge_window_optimised = {}
        for window_n in range(len(charge_windows)):
            for minute in range(charge_windows[window_n]["start"], charge_windows[window_n]["end"], PREDICT_STEP):
                if is_export and charge_limit[window_n] < EXPORT_LIMIT_IDLE:
                    charge_window_optimised[minute] = window_n
                elif not is_export and charge_limit[window_n] > 0.0:
                    charge_window_optimised[minute] = window_n
        return charge_window_optimised

    def run_prediction(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, save=None, step=PREDICT_STEP, cache=False):
        """
        Run a prediction scenario given a charge limit, return the results

        PARITY RULE: The hot loop below is mirrored by the C++ kernel in prediction_kernel.cpp
        for the scenarios it supports. Any behavioural change here MUST be mirrored there,
        KERNEL_PARITY_REVISION (prediction_kernel.py) and PK_PARITY_REVISION (prediction_kernel.cpp)
        must both be bumped, and the kernel_parity test must pass (cd coverage && ./run_all --test kernel_parity).
        """
        # A saving run publishes predict_soc_best and friends, and it is the only run that does. If a
        # batch were left pending, the next handle read would flush it and reset_kernel_run_state
        # would blank exactly those attributes, emptying the published plan and the debug HTML; a job
        # flushed after its inputs were mutated would also be cached under a key hashed from the old
        # ones, poisoning the shared cache. Draining first makes both impossible. Every fan-out site
        # already drains its handles before saving, so this is the class defending its own invariant
        # rather than a live fix - and it costs the ~250k non-save calls a single test of a local.
        if save and self.pending_batch:
            self.flush_batch()

        # The cache key is only wanted when the cache is actually in play - a saving run always
        # simulates - so it is not computed otherwise. Building it as one tuple hash keeps the
        # per-window hashing in C rather than looping in Python, which matters because this runs on
        # every simulation with a few hundred windows.
        sim_hash = None
        if cache and not save:
            sim_hash = prediction_cache_key(charge_limit, charge_window, export_limits, export_window, pv_scenario, end_record, step)
            cached_result = self.prediction_cache.get(sim_hash)
            if cached_result is not None:
                # Return cached result
                return cached_result

        # Try the C++ prediction kernel first; unsupported scenarios fall through to the Python engine.
        # The kernel understands all three pv_scenario values (see PkScenario.pv_scenario, ABI 3).
        if kernel_supported(self, save, step):
            kernel_result = run_prediction_kernel(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, cache)
            if kernel_result is not None:
                if sim_hash is not None:
                    # Store in cache without the SoC/car data to save memory, mirroring the Python engine
                    self.prediction_cache[sim_hash] = kernel_result[:11] + ([], []) + kernel_result[13:]
                return kernel_result

        # Fetch data from globals, optimised away from class to avoid passing it between threads
        if pv_scenario == PV_SCENARIO_PV10:
            pv_forecast_minute_step = self.pv_forecast_minute10_step
            load_minutes_step = self.load_minutes_step10
        elif pv_scenario == PV_SCENARIO_PV90:
            pv_forecast_minute_step = self.pv_forecast_minute90_step
            load_minutes_step = self.load_minutes_step90
        else:
            pv_forecast_minute_step = self.pv_forecast_minute_step
            load_minutes_step = self.load_minutes_step

        rate_import = self.rate_import
        rate_export = self.rate_export
        io_adjusted = self.io_adjusted

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
        clipping_penalty_total = 0
        predict_soc = {}
        car_charging_soc_next = self.car_charging_soc_next[:]
        iboost_next = self.iboost_next
        iboost_running = self.iboost_running
        iboost_running_solar = self.iboost_running_solar
        iboost_running_full = self.iboost_running_full
        car_load_energy_bypass = 0

        # Remove intersecting windows and optimise the data format of the charge/discharge window
        charge_limit, charge_window = remove_intersecting_windows(charge_limit, charge_window, export_limits, export_window)
        charge_window_optimised = self.find_charge_window_optimised(charge_window, charge_limit)
        export_window_optimised = self.find_charge_window_optimised(export_window, export_limits, is_export=True)

        # For the SoC calculation we need to stop 24 hours after the first charging window starts
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

        enable_standing_charge = save and (save in ["best", "base", "base10", "best10", "test", "yesterday", "yesterday10"])
        enable_save_stats = save and (save in ["best", "test", "compare", "yesterday"])
        car_enable = self.num_cars > 0
        car_energy_reported_load = self.car_energy_reported_load
        inverter_limit = self.inverter_limit * step
        export_limit = self.export_limit * step
        pv_ac_limit = self.pv_ac_limit * step
        set_charge_low_power = self.set_charge_window and self.set_charge_low_power and (save in ["best", "best10", "test"])
        carbon_enable = self.carbon_enable
        # clipping_limit is in kW. We need the energy limit in kWh for the given step.
        reserve = self.reserve
        soc_max = self.soc_max
        reserve_percent = calc_percent_limit(reserve, soc_max)
        battery_loss = self.battery_loss
        battery_loss_discharge = self.battery_loss_discharge
        battery_temperature_prediction = self.battery_temperature_prediction
        all_active_keep = self.all_active_keep
        best_soc_keep_weight = self.best_soc_keep_weight
        best_soc_keep_orig = self.best_soc_keep
        debug_enable = self.debug_enable
        set_reserve_enable = self.set_reserve_enable
        set_export_freeze = self.set_export_freeze
        set_export_freeze_only = self.set_export_freeze_only
        set_charge_window = self.set_charge_window
        set_export_window = self.set_export_window
        battery_rate_max_charge = self.battery_rate_max_charge
        battery_rate_max_charge_dc = self.battery_rate_max_charge_dc
        battery_rate_max_discharge = self.battery_rate_max_discharge
        battery_rate_max_export = self.battery_rate_max_export
        battery_rate_min = self.battery_rate_min
        inverter_freeze_export_discharge_rate = self.inverter_freeze_export_discharge_rate
        carbon_intensity = self.carbon_intensity
        set_discharge_during_charge = self.set_discharge_during_charge
        battery_charge_power_curve_tuple = charge_curve_to_tuple(self.battery_charge_power_curve)
        battery_discharge_power_curve_tuple = charge_curve_to_tuple(self.battery_discharge_power_curve)
        battery_temperature_charge_curve_tuple = charge_curve_to_tuple(self.battery_temperature_charge_curve)
        battery_temperature_discharge_curve_tuple = charge_curve_to_tuple(self.battery_temperature_discharge_curve)
        calculate_export_on_pv = self.calculate_export_on_pv

        # For the PV10 case we apply some de-rating to the battery charge rate to be more pessimistic.
        # PV90 is the upside case and gets no de-rate.
        if pv_scenario == PV_SCENARIO_PV10:
            battery_rate_max_scaling = self.battery_rate_max_scaling * self.charge_scaling10
        else:
            battery_rate_max_scaling = self.battery_rate_max_scaling

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

        # PV forecast remaining from each step to the end of the forecast, used to work out how much PV a charge
        # window still overlaps with as low power charging must be abandoned when the sun is contributing
        pv_remaining_kwh = {}
        if set_charge_low_power:
            pv_remaining = 0.0
            for minute_step in range(((self.forecast_minutes - 1) // step) * step, -1, -step):
                pv_remaining += pv_forecast_minute_step_flat.get(minute_step, 0.0)
                pv_remaining_kwh[minute_step] = pv_remaining

        # Simulate each forward minute
        minute = 0
        while minute < self.forecast_minutes:
            # Minute yesterday can wrap if days_previous is only 1
            minute_absolute = minute + self.minutes_now
            prev_soc = soc
            reserve_expected = reserve
            import_rate = rate_import.get(minute_absolute, 0)
            if io_adjusted.get(minute_absolute, 0) and pv_scenario == PV_SCENARIO_PV10 and minute > 30:
                import_rate = self.rate_max  # Assume in worst case that slot goes away and max rate applies
            export_rate = rate_export.get(minute_absolute, 0)

            # Alert?
            alert_keep = all_active_keep.get(minute_absolute, 0)

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
                keep_minute_scaling = max(keep_minute_scaling, 10.0)
                best_soc_keep = max(best_soc_keep, min(alert_keep / 100.0 * soc_max, soc_max))

            # Find charge & discharge windows
            minute_absolute_aligned = int(minute_absolute / step) * step
            charge_window_n = charge_window_optimised.get(minute_absolute_aligned, -1)
            export_window_n = export_window_optimised.get(minute_absolute_aligned, -1)
            charge_window_active = charge_window_n >= 0
            export_window_active = export_window_n >= 0
            export_limit_now = export_limits[export_window_n] if export_window_active else EXPORT_LIMIT_IDLE

            # Find charge limit
            charge_limit_n = 0
            if charge_window_active:
                charge_limit_n = charge_limit[charge_window_n]
                if self.set_charge_freeze and (calc_percent_limit(charge_limit_n, soc_max) == reserve_percent):
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

