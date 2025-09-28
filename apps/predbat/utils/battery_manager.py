# -----------------------------------------------------------------------------
# Predbat Home Battery System - Battery Manager Class
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Object-oriented battery management class following SOLID principles.

This class encapsulates all battery-related calculations including SOC management,
charge/discharge rate curves, temperature compensation, and optimization algorithms.
"""

from typing import Dict, List, Union, Optional, Callable, Tuple, Any
from dataclasses import dataclass


@dataclass
class BatteryConfig:
    """Configuration class for battery parameters."""

    minute_watt: int = 60000  # 60 * 1000
    predict_step: int = 5

    # Battery limits
    rate_min: float = 0.0
    rate_max_charge: float = 5.0
    rate_max_discharge: float = 5.0
    rate_max_scaling: float = 1.0

    # Efficiency and loss
    efficiency: float = 0.95
    loss: float = 0.95

    # Temperature range
    temp_min: int = -20
    temp_max: int = 20


class BatteryManager:
    """
    Manages all battery-related calculations and operations.

    This class follows SOLID principles by:
    - Single Responsibility: Only handles battery calculations
    - Open/Closed: Extensible through inheritance and composition
    - Liskov Substitution: Can be replaced by subclasses
    - Interface Segregation: Clean, focused public interface
    - Dependency Inversion: Depends on abstractions (config), not concrete implementations
    """

    def __init__(self, config: Optional[BatteryConfig] = None):
        """
        Initialize battery manager with configuration.

        Args:
            config: Battery configuration. Uses defaults if None.
        """
        self._config = config or BatteryConfig()

    @property
    def config(self) -> BatteryConfig:
        """Get current battery configuration."""
        return self._config

    def update_config(self, **kwargs) -> None:
        """Update configuration parameters."""
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)
            else:
                raise ValueError(f"Unknown config parameter: {key}")

    # SOC and Percentage Calculations
    def calculate_soc_percentage(self, charge_kwh: float, capacity_kwh: float) -> float:
        """Calculate State of Charge percentage from charge and capacity."""
        if capacity_kwh <= 0 or charge_kwh < 0:
            return 0.0
        return min(100.0, (charge_kwh / capacity_kwh) * 100.0)

    def calculate_charge_from_percentage(self, soc_percent: float, capacity_kwh: float) -> float:
        """Calculate charge in kWh from SOC percentage and capacity."""
        if capacity_kwh <= 0:
            return 0.0
        return min(capacity_kwh, (soc_percent / 100.0) * capacity_kwh)

    def calc_percent_limit(self, charge_limit: Union[float, List[float]], soc_max: float) -> Union[int, List[int]]:
        """Calculate a charge limit in percent."""
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

    # Time Calculations
    def calculate_charge_time_minutes(self, kwh_needed: float, charge_rate_kw: float) -> int:
        """Calculate minutes needed to charge given kWh at specified rate."""
        if charge_rate_kw <= 0 or kwh_needed <= 0:
            return 0
        return int((kwh_needed / charge_rate_kw) * 60)

    def calculate_discharge_time_minutes(self, kwh_available: float, discharge_rate_kw: float) -> int:
        """Calculate minutes available to discharge given kWh at specified rate."""
        if discharge_rate_kw <= 0 or kwh_available <= 0:
            return 0
        return int((kwh_available / discharge_rate_kw) * 60)

    # Efficiency and Loss
    def apply_efficiency_loss(self, kwh: float, efficiency: Optional[float] = None) -> float:
        """Apply inverter efficiency loss to energy amount."""
        eff = efficiency if efficiency is not None else self._config.efficiency
        if eff < 0:
            eff = 0.0
        elif eff > 1.0:
            eff = 1.0
        return kwh * eff

    # Battery State Checks
    def is_battery_full(self, soc_percent: float, tolerance: float = 0.1) -> bool:
        """Check if battery is considered full within tolerance."""
        return soc_percent >= (100.0 - tolerance)

    def is_battery_empty(self, soc_percent: float, tolerance: float = 0.1) -> bool:
        """Check if battery is considered empty within tolerance."""
        return soc_percent <= tolerance

    def clamp_soc_to_limits(self, soc_percent: float, min_soc: float = 0.0, max_soc: float = 100.0) -> float:
        """Clamp SOC percentage to specified limits."""
        return max(min_soc, min(max_soc, soc_percent))

    # Temperature Compensation
    def find_battery_temperature_cap(self, battery_temperature: float, battery_temperature_curve: Dict[int, float], soc_max: float, max_rate: float) -> float:
        """Find the battery temperature cap for charge/discharge rates."""
        battery_temperature_idx = min(battery_temperature, self._config.temp_max)
        battery_temperature_idx = max(battery_temperature_idx, self._config.temp_min)

        if battery_temperature_idx in battery_temperature_curve:
            battery_temperature_adjust = battery_temperature_curve[battery_temperature_idx]
        elif battery_temperature_idx > 0:
            battery_temperature_adjust = battery_temperature_curve.get(self._config.temp_max, 1.0)
        else:
            battery_temperature_adjust = battery_temperature_curve.get(0, 1.0)

        battery_temperature_rate_cap = soc_max * battery_temperature_adjust / 60.0
        return min(battery_temperature_rate_cap, max_rate)

    # Battery Curve Calculations
    def get_charge_rate_curve(
        self,
        soc: float,
        charge_rate_setting: float,
        soc_max: float,
        battery_rate_max_charge: Optional[float] = None,
        battery_charge_power_curve: Optional[Dict[int, float]] = None,
        battery_rate_min: Optional[float] = None,
        battery_temperature: float = 20,
        battery_temperature_curve: Optional[Dict[int, float]] = None,
        debug: bool = False,
    ) -> float:
        """Compute true charging rate from SOC and charge rate setting."""
        # Use config defaults if not provided
        battery_rate_max_charge = battery_rate_max_charge or self._config.rate_max_charge
        battery_charge_power_curve = battery_charge_power_curve or {}
        battery_rate_min = battery_rate_min or self._config.rate_min
        battery_temperature_curve = battery_temperature_curve or {}

        soc_percent = self.calc_percent_limit(soc, soc_max)
        max_charge_rate = battery_rate_max_charge * battery_charge_power_curve.get(soc_percent, 1.0)

        # Temperature cap
        max_rate_cap = self.find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, battery_rate_max_charge)
        max_charge_rate = min(max_charge_rate, max_rate_cap)

        if debug:
            print(
                "Max charge rate: {} SOC: {} Percent {} Rate in: {} rate out: {} cap: {}".format(
                    max_charge_rate * self._config.minute_watt, soc, soc_percent, charge_rate_setting * self._config.minute_watt, min(charge_rate_setting, max_charge_rate) * self._config.minute_watt, max_rate_cap * self._config.minute_watt
                )
            )
        return max(min(charge_rate_setting, max_charge_rate), battery_rate_min)

    def get_discharge_rate_curve(
        self,
        soc: float,
        discharge_rate_setting: float,
        soc_max: float,
        battery_rate_max_discharge: Optional[float] = None,
        battery_discharge_power_curve: Optional[Dict[int, float]] = None,
        battery_rate_min: Optional[float] = None,
        battery_temperature: float = 20,
        battery_temperature_curve: Optional[Dict[int, float]] = None,
    ) -> float:
        """Compute true discharging rate from SOC and discharge rate setting."""
        # Use config defaults if not provided
        battery_rate_max_discharge = battery_rate_max_discharge or self._config.rate_max_discharge
        battery_discharge_power_curve = battery_discharge_power_curve or {}
        battery_rate_min = battery_rate_min or self._config.rate_min
        battery_temperature_curve = battery_temperature_curve or {}

        soc_percent = self.calc_percent_limit(soc, soc_max)
        max_discharge_rate = battery_rate_max_discharge * battery_discharge_power_curve.get(soc_percent, 1.0)
        max_rate_cap = self.find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, battery_rate_max_discharge)
        max_discharge_rate = min(max_discharge_rate, max_rate_cap)

        return max(min(discharge_rate_setting, max_discharge_rate), battery_rate_min)

    # Complex Optimization
    def find_charge_rate(
        self,
        minutes_now: int,
        soc: float,
        window: Dict[str, Any],
        target_soc: float,
        max_rate: float,
        soc_max: float,
        battery_charge_power_curve: Optional[Dict[int, float]] = None,
        set_charge_low_power: bool = True,
        charge_low_power_margin: int = 15,
        battery_rate_min: Optional[float] = None,
        battery_rate_max_scaling: Optional[float] = None,
        battery_loss: Optional[float] = None,
        log_to: Optional[Callable[[str], None]] = None,
        battery_temperature: float = 20,
        battery_temperature_curve: Optional[Dict[int, float]] = None,
        current_charge_rate: Optional[float] = None,
    ) -> Tuple[float, float]:
        """Find the lowest charge rate that fits the charge window."""
        # Use config defaults if not provided
        battery_charge_power_curve = battery_charge_power_curve or {}
        battery_rate_min = battery_rate_min or self._config.rate_min
        battery_rate_max_scaling = battery_rate_max_scaling or self._config.rate_max_scaling
        battery_loss = battery_loss or self._config.loss
        battery_temperature_curve = battery_temperature_curve or {}

        margin = charge_low_power_margin
        target_soc = round(target_soc, 2)

        # Current charge rate
        if current_charge_rate is None:
            current_charge_rate = max_rate

        # Real achieved max rate
        max_rate_real = self.get_charge_rate_curve(soc, max_rate, soc_max, self._config.rate_max_charge, battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve) * battery_rate_max_scaling

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
            min_rate_w = int(min_rate * self._config.minute_watt)

            # Apply the curve at each rate to pick one that works
            rate_w = max_rate * self._config.minute_watt
            best_rate = max_rate
            best_rate_real = max_rate_real
            highest_achievable_rate = 0

            if log_to:
                log_to(
                    "Find charge rate for low power mode: soc: {} target_soc: {} charge_left: {} minutes_left: {} abs_minutes_left: {} max_rate: {} min_rate: {} min_rate_w: {}".format(
                        soc, target_soc, charge_left, minutes_left, abs_minutes_left, max_rate * self._config.minute_watt, min_rate * self._config.minute_watt, min_rate_w
                    )
                )

            while rate_w >= 400:
                rate = rate_w / self._config.minute_watt
                if rate_w >= min_rate_w:
                    charge_now = soc
                    minute = 0
                    rate_scale_max = 0
                    # Compute over the time period, include the completion time
                    for minute in range(0, minutes_left, self._config.predict_step):
                        rate_scale = self.get_charge_rate_curve(charge_now, rate, soc_max, self._config.rate_max_charge, battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve)
                        highest_achievable_rate = max(highest_achievable_rate, rate_scale)
                        rate_scale *= battery_rate_max_scaling
                        rate_scale_max = max(rate_scale_max, rate_scale)
                        charge_amount = rate_scale * self._config.predict_step * battery_loss
                        charge_now += charge_amount
                        if (round(charge_now, 2) >= target_soc) and (rate_scale_max < best_rate_real):
                            best_rate = rate
                            best_rate_real = rate_scale_max
                            break
                else:
                    break
                rate_w -= 100.0

            # Stick with current rate if it doesn't matter
            if best_rate >= highest_achievable_rate and current_charge_rate >= highest_achievable_rate:
                best_rate = current_charge_rate
                if log_to:
                    log_to(
                        "Low Power mode: best rate {} is greater than highest achievable rate {} and current rate {} so sticking with current rate".format(
                            best_rate * self._config.minute_watt, highest_achievable_rate * self._config.minute_watt, current_charge_rate * self._config.minute_watt
                        )
                    )

            best_rate_real = self.get_charge_rate_curve(soc, best_rate, soc_max, self._config.rate_max_charge, battery_charge_power_curve, battery_rate_min, battery_temperature, battery_temperature_curve) * battery_rate_max_scaling

            if log_to:
                log_to(
                    "Low Power mode: minutes left: {} absolute: {} SOC: {} Target SOC: {} Charge left: {} Max rate: {} Min rate: {} Best rate: {} Best rate real: {} Battery temp {}".format(
                        minutes_left,
                        abs_minutes_left,
                        soc,
                        target_soc,
                        charge_left,
                        max_rate * self._config.minute_watt,
                        min_rate * self._config.minute_watt,
                        best_rate * self._config.minute_watt,
                        best_rate_real * self._config.minute_watt,
                        battery_temperature,
                    )
                )
            return best_rate, best_rate_real
        else:
            return max_rate, max_rate_real

    def calculate_energy_needed_for_soc(self, current_soc: float, target_soc: float, capacity_kwh: float) -> float:
        """Calculate energy needed to reach target SOC from current SOC."""
        if capacity_kwh <= 0:
            return 0.0

        current_kwh = self.calculate_charge_from_percentage(current_soc, capacity_kwh)
        target_kwh = self.calculate_charge_from_percentage(target_soc, capacity_kwh)

        return target_kwh - current_kwh

    def calculate_cycle_equivalent(self, energy_kwh: float, capacity_kwh: float) -> float:
        """Calculate battery cycle equivalent from energy throughput."""
        if capacity_kwh <= 0:
            return 0.0
        return energy_kwh / capacity_kwh
