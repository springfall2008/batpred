# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Vectorized batch prediction engine for fast simulation of multiple scenarios.
Uses NumPy array operations to evaluate hundreds of charge/discharge window
combinations simultaneously. Designed for levels pass optimization.
"""

import numpy as np
from const import PREDICT_STEP


class PredictionVectorized:
    """
    Vectorized prediction engine that runs multiple scenarios in parallel using NumPy.
    Simplified physics model:
    - AC-only inverter (no hybrid DC path)
    - Single SOC lookup per time step for charge curves
    - No iboost, temperature effects, or other advanced features
    - Suitable for initial filtering in levels pass optimization
    """

    def __init__(self, base, step_minutes=30):
        """
        Initialize vectorized prediction from base Prediction object.

        Args:
            base: Prediction object with configuration and forecasts
            step_minutes: Time step size in minutes (default 30)
        """
        self.log = base.log
        self.step_minutes = step_minutes
        self.minutes_now = base.minutes_now
        self.forecast_minutes = base.forecast_minutes

        # Battery parameters
        self.soc_kw = base.soc_kw
        self.soc_max = base.soc_max
        self.reserve = base.reserve
        self.battery_loss = base.battery_loss
        self.battery_loss_discharge = base.battery_loss_discharge
        self.battery_rate_max_charge = base.battery_rate_max_charge
        self.battery_rate_max_discharge = base.battery_rate_max_discharge
        self.battery_rate_min = base.battery_rate_min
        self.battery_rate_max_scaling = base.battery_rate_max_scaling
        self.battery_rate_max_scaling_discharge = base.battery_rate_max_scaling_discharge

        # Inverter parameters
        self.inverter_loss = base.inverter_loss
        self.inverter_limit = base.inverter_limit
        self.export_limit = base.export_limit

        # Cost tracking
        self.cost_today_sofar = base.cost_today_sofar
        self.import_today_now = base.import_today_now
        self.export_today_now = base.export_today_now

        # Power curves (simplified)
        self.battery_charge_power_curve = base.battery_charge_power_curve
        self.battery_discharge_power_curve = base.battery_discharge_power_curve

        # Prepare arrays
        self.pv_array = None
        self.load_array = None
        self.rate_import_array = None
        self.rate_export_array = None
        self.num_steps = 0

        self.log("Vectorized prediction initialized with step_minutes={}".format(step_minutes))

    def prepare_forecast_arrays(self, pv_forecast_minute_step, load_minutes_step):
        """
        Convert forecast dictionaries to NumPy arrays aggregated to step_minutes.
        Merges car charging into load data.

        Args:
            pv_forecast_minute_step: Dict of {minute: kW} for PV forecast
            load_minutes_step: Dict of {minute: kW} for load forecast

        Returns:
            Tuple of (pv_array, load_array, num_steps)
        """
        self.num_steps = int(self.forecast_minutes / self.step_minutes)
        self.pv_array = np.zeros(self.num_steps)
        self.load_array = np.zeros(self.num_steps)

        # Aggregate to larger time steps
        for step_idx in range(self.num_steps):
            minute_start = step_idx * self.step_minutes
            pv_sum = 0.0
            load_sum = 0.0

            for offset in range(0, self.step_minutes, PREDICT_STEP):
                minute = minute_start + offset
                if minute >= self.forecast_minutes:
                    break
                pv_sum += pv_forecast_minute_step.get(minute, 0.0)
                load_sum += load_minutes_step.get(minute, 0.0)

            self.pv_array[step_idx] = pv_sum
            self.load_array[step_idx] = load_sum

        self.log("Prepared forecast arrays: {} steps of {} minutes".format(self.num_steps, self.step_minutes))
        return self.pv_array, self.load_array, self.num_steps

    def prepare_rate_arrays(self, rate_import, rate_export):
        """
        Convert rate dictionaries to NumPy arrays indexed by time step.

        Args:
            rate_import: Dict of {minute_absolute: £/kWh}
            rate_export: Dict of {minute_absolute: £/kWh}

        Returns:
            Tuple of (rate_import_array, rate_export_array)
        """
        self.rate_import_array = np.zeros(self.num_steps)
        self.rate_export_array = np.zeros(self.num_steps)

        for step_idx in range(self.num_steps):
            minute_start = step_idx * self.step_minutes
            minute_absolute = self.minutes_now + minute_start

            # Use rate at start of step (simplified)
            self.rate_import_array[step_idx] = rate_import.get(minute_absolute, 0.0)
            self.rate_export_array[step_idx] = rate_export.get(minute_absolute, 0.0)

        return self.rate_import_array, self.rate_export_array

    def prepare_window_masks(self, charge_windows, export_windows, num_scenarios):
        """
        Convert window lists and scenario bits into boolean masks.

        Args:
            charge_windows: List of {"start": minute, "end": minute} dicts
            export_windows: List of {"start": minute, "end": minute} dicts
            num_scenarios: Number of scenarios to generate

        Returns:
            Tuple of (charge_masks, export_masks) both shape (num_scenarios, num_steps)
        """
        num_charge_windows = len(charge_windows)
        num_export_windows = len(export_windows)

        # Create masks for each window (num_windows, num_steps)
        charge_window_masks = np.zeros((num_charge_windows, self.num_steps), dtype=bool)
        export_window_masks = np.zeros((num_export_windows, self.num_steps), dtype=bool)

        for w_idx, window in enumerate(charge_windows):
            start_step = max(0, int((window["start"] - self.minutes_now) / self.step_minutes))
            end_step = min(self.num_steps, int((window["end"] - self.minutes_now) / self.step_minutes))
            if start_step < end_step:
                charge_window_masks[w_idx, start_step:end_step] = True

        for w_idx, window in enumerate(export_windows):
            start_step = max(0, int((window["start"] - self.minutes_now) / self.step_minutes))
            end_step = min(self.num_steps, int((window["end"] - self.minutes_now) / self.step_minutes))
            if start_step < end_step:
                export_window_masks[w_idx, start_step:end_step] = True

        # For now, return window masks - caller will combine based on scenario bit patterns
        return charge_window_masks, export_window_masks

    def get_charge_rate(self, soc_array):
        """
        Get charge rate for given SOC values using simplified curve lookup.

        Args:
            soc_array: Array of SOC values in kWh (any shape)

        Returns:
            Array of charge rates in kW (same shape as input)
        """
        if not self.battery_charge_power_curve:
            # No curve, use max rate
            return np.full_like(soc_array, self.battery_rate_max_charge * self.battery_rate_max_scaling)

        # Extract curve points
        soc_points = np.array([point[0] * self.soc_max / 100.0 for point in self.battery_charge_power_curve])
        power_points = np.array([point[1] for point in self.battery_charge_power_curve])

        # Interpolate
        charge_rates = np.interp(soc_array, soc_points, power_points)
        charge_rates = charge_rates * self.battery_rate_max_scaling

        return charge_rates

    def get_discharge_rate(self, soc_array):
        """
        Get discharge rate for given SOC values using simplified curve lookup.

        Args:
            soc_array: Array of SOC values in kWh (any shape)

        Returns:
            Array of discharge rates in kW (same shape as input)
        """
        if not self.battery_discharge_power_curve:
            # No curve, use max rate
            return np.full_like(soc_array, self.battery_rate_max_discharge * self.battery_rate_max_scaling_discharge)

        # Extract curve points
        soc_points = np.array([point[0] * self.soc_max / 100.0 for point in self.battery_discharge_power_curve])
        power_points = np.array([point[1] for point in self.battery_discharge_power_curve])

        # Interpolate
        discharge_rates = np.interp(soc_array, soc_points, power_points)
        discharge_rates = discharge_rates * self.battery_rate_max_scaling_discharge

        return discharge_rates

    def run_prediction_batch(self, charge_window_enable, export_window_enable):
        """
        Run batch prediction for multiple scenarios.

        Args:
            charge_window_enable: Boolean array (num_scenarios, num_steps) - True where charging is forced
            export_window_enable: Boolean array (num_scenarios, num_steps) - True where discharging is forced

        Returns:
            Dict with keys:
                - final_cost: Array of final costs (num_scenarios,)
                - final_soc: Array of final SOC in kWh (num_scenarios,)
                - import_kwh: Array of total import (num_scenarios,)
                - export_kwh: Array of total export (num_scenarios,)
                - import_kwh_battery: Array of import for charging (num_scenarios,)
                - import_kwh_house: Array of import for load (num_scenarios,)
                - battery_cycle: Array of total throughput (num_scenarios,)
                - soc_min: Array of minimum SOC reached (num_scenarios,)
        """
        num_scenarios = charge_window_enable.shape[0]

        # Initialize state arrays (num_scenarios, num_steps+1)
        soc = np.full((num_scenarios, self.num_steps + 1), self.soc_kw)
        cost = np.full(num_scenarios, self.cost_today_sofar)
        import_kwh = np.full(num_scenarios, self.import_today_now)
        export_kwh = np.full(num_scenarios, self.export_today_now)
        import_kwh_battery = np.zeros(num_scenarios)
        import_kwh_house = np.zeros(num_scenarios)
        battery_cycle = np.zeros(num_scenarios)

        # Inverter and battery limits (scaled to step size)
        inverter_limit_step = self.inverter_limit * self.step_minutes
        export_limit_step = self.export_limit * self.step_minutes
        inverter_loss = self.inverter_loss

        # Time loop (not vectorized over time, but vectorized over scenarios)
        for step_idx in range(self.num_steps):
            soc_current = soc[:, step_idx]

            # Get PV and load for this step
            pv_now = self.pv_array[step_idx]
            load_now = self.load_array[step_idx]

            # Get rates
            import_rate = self.rate_import_array[step_idx]
            export_rate = self.rate_export_array[step_idx]

            # Get charge/discharge windows for this step
            charge_active = charge_window_enable[:, step_idx]  # (num_scenarios,)
            export_active = export_window_enable[:, step_idx]  # (num_scenarios,)

            # Get SOC-dependent rates
            charge_rate = self.get_charge_rate(soc_current)  # (num_scenarios,)
            discharge_rate = self.get_discharge_rate(soc_current)  # (num_scenarios,)

            # Scale to step size
            charge_rate_step = charge_rate * self.step_minutes
            discharge_rate_step = discharge_rate * self.step_minutes

            # Calculate battery capacity limits
            battery_to_min = np.maximum(soc_current - self.reserve, 0) * self.battery_loss_discharge
            battery_to_max = np.maximum(self.soc_max - soc_current, 0) * self.battery_loss

            # Initialize battery draw
            battery_draw = np.zeros(num_scenarios)

            # Mode 1: Force discharge (export window active)
            force_discharge = export_active
            battery_draw = np.where(force_discharge, np.minimum(discharge_rate_step, battery_to_min), battery_draw)

            # Mode 2: Force charge (charge window active, not discharge)
            force_charge = charge_active & ~export_active
            battery_draw = np.where(force_charge, -np.minimum(charge_rate_step, battery_to_max), battery_draw)

            # Mode 3: ECO mode (no windows active)
            eco_mode = ~charge_active & ~export_active

            # For ECO mode: calculate PV AC and determine battery action
            pv_ac = pv_now * inverter_loss  # AC-only inverter
            diff_eco = load_now - pv_ac  # Shortfall (positive) or excess (negative)

            # If shortfall, discharge to meet it
            battery_draw_eco = np.where(
                diff_eco > 0,
                np.minimum(np.minimum(diff_eco, discharge_rate_step), battery_to_min),
                # If excess, charge from it
                np.maximum(np.maximum(diff_eco, -charge_rate_step), -battery_to_max),
            )

            battery_draw = np.where(eco_mode, battery_draw_eco, battery_draw)

            # Apply inverter limit (AC-only, simplified)
            # Limit discharge
            battery_draw = np.where(battery_draw > 0, np.minimum(battery_draw, inverter_limit_step), battery_draw)
            # Limit charge
            battery_draw = np.where(battery_draw < 0, np.maximum(battery_draw, -inverter_limit_step), battery_draw)

            # Update SOC with asymmetric losses
            soc_delta = np.where(battery_draw > 0, -battery_draw / self.battery_loss_discharge, -battery_draw * self.battery_loss)  # Discharge  # Charge (battery_draw is negative)

            soc_next = soc_current + soc_delta
            soc_next = np.clip(soc_next, self.reserve, self.soc_max)
            soc[:, step_idx + 1] = soc_next

            # Calculate grid import/export (AC-only model)
            # Grid balance = load - pv - battery (positive battery_draw = discharge helps, negative = charge consumes)
            grid_balance = load_now - pv_ac - battery_draw / inverter_loss

            # Positive grid_balance = import, negative = export
            step_import = np.maximum(grid_balance, 0)
            step_export = np.maximum(-grid_balance, 0)

            # Limit export
            step_export = np.minimum(step_export, export_limit_step)

            # Update cumulative energy
            import_kwh += step_import
            export_kwh += step_export

            # Track battery vs house import
            import_kwh_battery += np.where(charge_active, step_import, 0)
            import_kwh_house += np.where(~charge_active, step_import, 0)

            # Update cost
            cost += step_import * import_rate - step_export * export_rate

            # Update battery cycles
            battery_cycle += np.abs(battery_draw)

        # Calculate minimum SOC
        soc_min = np.min(soc[:, :-1], axis=1)

        # Return results
        return {
            "final_cost": cost,
            "final_soc": soc[:, -1],
            "import_kwh": import_kwh,
            "export_kwh": export_kwh,
            "import_kwh_battery": import_kwh_battery,
            "import_kwh_house": import_kwh_house,
            "battery_cycle": battery_cycle,
            "soc_min": soc_min,
            "soc_trajectories": soc,  # For debugging
        }


# Test harness
if __name__ == "__main__":
    print("Vectorized Prediction Test Harness")
    print("=" * 60)

    # Create a dummy base object
    class DummyBase:
        def __init__(self):
            self.minutes_now = 0
            self.forecast_minutes = 2880  # 48 hours
            self.soc_kw = 5.0
            self.soc_max = 10.0
            self.reserve = 1.0
            self.battery_loss = 0.97
            self.battery_loss_discharge = 0.97
            self.battery_rate_max_charge = 3.0  # kW
            self.battery_rate_max_discharge = 3.0  # kW
            self.battery_rate_min = 0.0
            self.battery_rate_max_scaling = 1.0
            self.battery_rate_max_scaling_discharge = 1.0
            self.inverter_loss = 0.96
            self.inverter_limit = 3.5  # kW
            self.export_limit = 3.5  # kW
            self.cost_today_sofar = 0.0
            self.import_today_now = 0.0
            self.export_today_now = 0.0

            # Simple power curves (SOC % -> power factor)
            self.battery_charge_power_curve = [[0, 1.0], [50, 1.0], [90, 0.8], [100, 0.3]]
            self.battery_discharge_power_curve = [[0, 0.3], [10, 0.8], [50, 1.0], [100, 1.0]]

        def log(self, msg):
            print("[LOG] {}".format(msg))

    # Create vectorized predictor
    base = DummyBase()
    predictor = PredictionVectorized(base, step_minutes=30)

    # Create synthetic forecasts
    pv_forecast = {}
    load_forecast = {}

    for minute in range(0, 2880, 5):
        hour = (minute // 60) % 24
        # Simple sinusoidal PV (peak at noon)
        if 6 <= hour < 18:
            pv_forecast[minute] = 0.5 * (1 + np.sin((hour - 6) * np.pi / 12))
        else:
            pv_forecast[minute] = 0.0

        # Simple load pattern
        if 7 <= hour < 9 or 17 <= hour < 22:
            load_forecast[minute] = 0.8
        else:
            load_forecast[minute] = 0.3

    predictor.prepare_forecast_arrays(pv_forecast, load_forecast)

    # Create synthetic rates
    rate_import = {}
    rate_export = {}
    for minute in range(0, 2880, 5):
        hour = (minute // 60) % 24
        # Cheap overnight, expensive peak
        if 2 <= hour < 5:
            rate_import[minute] = 0.075  # Cheap
        elif 16 <= hour < 19:
            rate_import[minute] = 0.30  # Expensive
        else:
            rate_import[minute] = 0.15  # Mid

        rate_export[minute] = 0.05

    predictor.prepare_rate_arrays(rate_import, rate_export)

    # Create test windows
    charge_windows = [
        {"start": 120, "end": 300},  # 02:00-05:00
    ]
    export_windows = [
        {"start": 960, "end": 1140},  # 16:00-19:00
    ]

    charge_window_masks, export_window_masks = predictor.prepare_window_masks(charge_windows, export_windows, num_scenarios=4)

    # Create 4 test scenarios (combinations of windows on/off)
    # Scenario 0: No windows
    # Scenario 1: Charge only
    # Scenario 2: Export only
    # Scenario 3: Both windows

    num_scenarios = 4
    charge_enable = np.zeros((num_scenarios, predictor.num_steps), dtype=bool)
    export_enable = np.zeros((num_scenarios, predictor.num_steps), dtype=bool)

    # Scenario 1: Charge window enabled
    charge_enable[1, :] = charge_window_masks[0, :]

    # Scenario 2: Export window enabled
    export_enable[2, :] = export_window_masks[0, :]

    # Scenario 3: Both enabled
    charge_enable[3, :] = charge_window_masks[0, :]
    export_enable[3, :] = export_window_masks[0, :]

    # Run batch prediction
    print("\nRunning batch prediction for {} scenarios...".format(num_scenarios))
    results = predictor.run_prediction_batch(charge_enable, export_enable)

    # Display results
    print("\nResults:")
    print("-" * 60)
    for i in range(num_scenarios):
        scenario_name = ["ECO only (no windows)", "Charge window only", "Export window only", "Both windows"][i]

        print("\nScenario {}: {}".format(i, scenario_name))
        print("  Final cost:     £{:.2f}".format(results["final_cost"][i]))
        print("  Final SOC:      {:.2f} kWh".format(results["final_soc"][i]))
        print("  Min SOC:        {:.2f} kWh".format(results["soc_min"][i]))
        print("  Import (total): {:.2f} kWh".format(results["import_kwh"][i]))
        print("  Import (batt):  {:.2f} kWh".format(results["import_kwh_battery"][i]))
        print("  Import (house): {:.2f} kWh".format(results["import_kwh_house"][i]))
        print("  Export:         {:.2f} kWh".format(results["export_kwh"][i]))
        print("  Battery cycle:  {:.2f} kWh".format(results["battery_cycle"][i]))

    # Find best scenario
    best_idx = np.argmin(results["final_cost"])
    print("\n" + "=" * 60)
    print("Best scenario: {} (£{:.2f})".format(["ECO only", "Charge only", "Export only", "Both windows"][best_idx], results["final_cost"][best_idx]))
    print("=" * 60)
