# -----------------------------------------------------------------------------
# Predbat Home Battery System - Battery Curve Utilities
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Battery curve and temperature compensation functions extracted from utils.py

This module contains functions for calculating actual battery charge/discharge
rates based on SOC curves and temperature compensation.
"""

from typing import Dict
from .battery_calculations import calc_percent_limit


def get_charge_rate_curve(
    soc: float,
    charge_rate_setting: float,
    soc_max: float,
    battery_rate_max_charge: float,
    battery_charge_power_curve: Dict[int, float],
    battery_rate_min: float,
    battery_temperature: float,
    battery_temperature_curve: Dict[int, float],
    debug: bool = False,
    minute_watt: int = 60000,
) -> float:
    """
    Compute true charging rate from SOC and charge rate setting.

    Takes into account SOC-based power curves and temperature compensation
    to determine the actual achievable charge rate.

    Args:
        soc: Current state of charge in kWh
        charge_rate_setting: Requested charge rate in kW
        soc_max: Maximum battery capacity in kWh
        battery_rate_max_charge: Maximum charge rate in kW
        battery_charge_power_curve: SOC percentage to power factor mapping
        battery_rate_min: Minimum allowed charge rate in kW
        battery_temperature: Battery temperature in Celsius
        battery_temperature_curve: Temperature to power factor mapping
        debug: Enable debug output

    Returns:
        Actual achievable charge rate in kW

    Examples:
        >>> curve = {50: 1.0, 80: 0.8, 90: 0.5}
        >>> temp_curve = {20: 1.0, 0: 0.8, -10: 0.6}
        >>> get_charge_rate_curve(5.0, 3.0, 10.0, 3.0, curve, 0.4, 20, temp_curve)
        3.0
    """
    soc_percent = calc_percent_limit(soc, soc_max)
    max_charge_rate = battery_rate_max_charge * battery_charge_power_curve.get(soc_percent, 1.0)

    # Temperature cap
    max_rate_cap = find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, battery_rate_max_charge)
    max_charge_rate = min(max_charge_rate, max_rate_cap)

    if debug:
        print(
            "Max charge rate: {} SOC: {} Percent {} Rate in: {} rate out: {} cap: {}".format(
                max_charge_rate * minute_watt, soc, soc_percent, charge_rate_setting * minute_watt, min(charge_rate_setting, max_charge_rate) * minute_watt, max_rate_cap * minute_watt
            )
        )
    return max(min(charge_rate_setting, max_charge_rate), battery_rate_min)


def get_discharge_rate_curve(
    soc: float, discharge_rate_setting: float, soc_max: float, battery_rate_max_discharge: float, battery_discharge_power_curve: Dict[int, float], battery_rate_min: float, battery_temperature: float, battery_temperature_curve: Dict[int, float]
) -> float:
    """
    Compute true discharging rate from SOC and discharge rate setting.

    Takes into account SOC-based power curves and temperature compensation
    to determine the actual achievable discharge rate.

    Args:
        soc: Current state of charge in kWh
        discharge_rate_setting: Requested discharge rate in kW
        soc_max: Maximum battery capacity in kWh
        battery_rate_max_discharge: Maximum discharge rate in kW
        battery_discharge_power_curve: SOC percentage to power factor mapping
        battery_rate_min: Minimum allowed discharge rate in kW
        battery_temperature: Battery temperature in Celsius
        battery_temperature_curve: Temperature to power factor mapping

    Returns:
        Actual achievable discharge rate in kW

    Examples:
        >>> curve = {50: 1.0, 20: 0.8, 10: 0.5}
        >>> temp_curve = {20: 1.0, 0: 0.8, -10: 0.6}
        >>> get_discharge_rate_curve(5.0, 3.0, 10.0, 3.0, curve, 0.4, 20, temp_curve)
        3.0
    """
    soc_percent = calc_percent_limit(soc, soc_max)
    max_discharge_rate = battery_rate_max_discharge * battery_discharge_power_curve.get(soc_percent, 1.0)
    max_rate_cap = find_battery_temperature_cap(battery_temperature, battery_temperature_curve, soc_max, battery_rate_max_discharge)
    max_discharge_rate = min(max_discharge_rate, max_rate_cap)

    return max(min(discharge_rate_setting, max_discharge_rate), battery_rate_min)


def find_battery_temperature_cap(battery_temperature: float, battery_temperature_curve: Dict[int, float], soc_max: float, max_rate: float) -> float:
    """
    Find the battery temperature cap for charge/discharge rates.

    Calculates the maximum allowed power based on battery temperature,
    using a temperature curve to determine derating factors.

    Args:
        battery_temperature: Battery temperature in Celsius
        battery_temperature_curve: Temperature to adjustment factor mapping
        soc_max: Maximum battery capacity in kWh
        max_rate: Maximum rate without temperature limits in kW

    Returns:
        Temperature-limited maximum rate in kW

    Examples:
        >>> temp_curve = {20: 1.0, 10: 0.9, 0: 0.8, -10: 0.6}
        >>> find_battery_temperature_cap(15, temp_curve, 10.0, 5.0)
        4.5  # Interpolated between 20°C (1.0) and 10°C (0.9)
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
