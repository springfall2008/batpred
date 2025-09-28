# -----------------------------------------------------------------------------
# Predbat Home Battery System - Charge Rate Calculation Utilities
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Complex charge rate calculation functions extracted from utils.py

This module contains the sophisticated charge rate optimization logic
for low-power charging modes and battery curve interactions.
"""

from typing import Dict, Any, Callable, Optional, Tuple
from config import MINUTE_WATT, PREDICT_STEP
from .battery_curves import get_charge_rate_curve


def find_charge_rate(
    minutes_now: int,
    soc: float,
    window: Dict[str, Any],
    target_soc: float,
    max_rate: float,
    soc_max: float,
    battery_charge_power_curve: Dict[int, float],
    set_charge_low_power: bool,
    charge_low_power_margin: int,
    battery_rate_min: float,
    battery_rate_max_scaling: float,
    battery_loss: float,
    log_to: Optional[Callable[[str], None]],
    battery_temperature: float = 20,
    battery_temperature_curve: Dict[int, float] = {},
    current_charge_rate: Optional[float] = None,
) -> Tuple[float, float]:
    """
    Find the lowest charge rate that fits the charge window.

    This is a sophisticated optimization function that calculates the minimum
    charge rate needed to reach a target SOC within a given time window,
    taking into account battery curves, temperature effects, and power scaling.

    Args:
        minutes_now: Current time in minutes from reference point
        soc: Current state of charge in kWh
        window: Dictionary with 'start', 'end' keys defining charge window
        target_soc: Target state of charge to reach in kWh
        max_rate: Maximum charge rate available in kW
        soc_max: Maximum battery capacity in kWh
        battery_charge_power_curve: SOC percentage to power factor mapping
        set_charge_low_power: Whether to enable low power optimization
        charge_low_power_margin: Safety margin in minutes for low power mode
        battery_rate_min: Minimum allowed charge rate in kW
        battery_rate_max_scaling: Scaling factor for maximum rates
        battery_loss: Efficiency loss factor (0.0-1.0)
        log_to: Optional logging function
        battery_temperature: Battery temperature in Celsius
        battery_temperature_curve: Temperature compensation curve
        current_charge_rate: Current charge rate to prefer if possible

    Returns:
        Tuple of (optimal_charge_rate, actual_achievable_rate)

    Examples:
        >>> window = {"start": 100, "end": 220}  # 2 hour window
        >>> curve = {50: 1.0, 80: 0.8, 90: 0.5}
        >>> rate, real_rate = find_charge_rate(
        ...     100, 5.0, window, 8.0, 3.0, 10.0, curve,
        ...     True, 15, 0.4, 1.0, 0.95, None
        ... )
        >>> # Returns optimized rate to reach 8kWh from 5kWh in 2 hours
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
