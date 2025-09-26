# -----------------------------------------------------------------------------
# Predbat Home Battery System - Battery Calculation Utilities
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Pure battery calculation functions extracted from plan.py

This module contains stateless, side-effect-free functions for battery calculations.
All functions are unit tested and maintain backward compatibility.
"""

from typing import List, Union


def calculate_soc_percentage(charge_kwh: float, capacity_kwh: float) -> float:
    """
    Calculate State of Charge percentage from charge and capacity.

    Args:
        charge_kwh: Current charge in kWh
        capacity_kwh: Battery capacity in kWh

    Returns:
        SOC percentage (0-100)

    Examples:
        >>> calculate_soc_percentage(5.0, 10.0)
        50.0
        >>> calculate_soc_percentage(12.0, 10.0)
        100.0
        >>> calculate_soc_percentage(5.0, 0.0)
        0.0
    """
    if capacity_kwh <= 0 or charge_kwh < 0:
        return 0.0
    return min(100.0, (charge_kwh / capacity_kwh) * 100.0)


def calculate_charge_from_percentage(soc_percent: float, capacity_kwh: float) -> float:
    """
    Calculate charge in kWh from SOC percentage and capacity.

    Args:
        soc_percent: State of charge percentage (0-100)
        capacity_kwh: Battery capacity in kWh

    Returns:
        Charge in kWh

    Examples:
        >>> calculate_charge_from_percentage(50.0, 10.0)
        5.0
        >>> calculate_charge_from_percentage(100.0, 10.0)
        10.0
    """
    if capacity_kwh <= 0:
        return 0.0
    return min(capacity_kwh, (soc_percent / 100.0) * capacity_kwh)


def calculate_charge_time_minutes(kwh_needed: float, charge_rate_kw: float) -> int:
    """
    Calculate minutes needed to charge given kWh at specified rate.

    Args:
        kwh_needed: Energy needed in kWh
        charge_rate_kw: Charge rate in kW

    Returns:
        Minutes needed to charge

    Examples:
        >>> calculate_charge_time_minutes(5.0, 2.5)
        120
        >>> calculate_charge_time_minutes(0.0, 2.5)
        0
        >>> calculate_charge_time_minutes(5.0, 0.0)
        0
    """
    if charge_rate_kw <= 0 or kwh_needed <= 0:
        return 0
    return int((kwh_needed / charge_rate_kw) * 60)


def calculate_discharge_time_minutes(kwh_available: float, discharge_rate_kw: float) -> int:
    """
    Calculate minutes available to discharge given kWh at specified rate.

    Args:
        kwh_available: Energy available in kWh
        discharge_rate_kw: Discharge rate in kW

    Returns:
        Minutes available to discharge

    Examples:
        >>> calculate_discharge_time_minutes(5.0, 2.5)
        120
        >>> calculate_discharge_time_minutes(0.0, 2.5)
        0
    """
    if discharge_rate_kw <= 0 or kwh_available <= 0:
        return 0
    return int((kwh_available / discharge_rate_kw) * 60)


def apply_efficiency_loss(kwh: float, efficiency: float) -> float:
    """
    Apply inverter efficiency loss to energy amount.

    Args:
        kwh: Energy amount in kWh
        efficiency: Efficiency as decimal (0.0-1.0), e.g., 0.95 for 95%

    Returns:
        Energy after efficiency loss

    Examples:
        >>> apply_efficiency_loss(10.0, 0.95)
        9.5
        >>> apply_efficiency_loss(10.0, 1.0)
        10.0
    """
    if efficiency < 0:
        efficiency = 0.0
    elif efficiency > 1.0:
        efficiency = 1.0

    return kwh * efficiency


def calculate_energy_needed_for_soc(current_soc: float, target_soc: float, capacity_kwh: float) -> float:
    """
    Calculate energy needed to reach target SOC from current SOC.

    Args:
        current_soc: Current SOC percentage (0-100)
        target_soc: Target SOC percentage (0-100)
        capacity_kwh: Battery capacity in kWh

    Returns:
        Energy needed in kWh (positive for charge, negative for discharge)

    Examples:
        >>> calculate_energy_needed_for_soc(50.0, 80.0, 10.0)
        3.0
        >>> calculate_energy_needed_for_soc(80.0, 50.0, 10.0)
        -3.0
    """
    if capacity_kwh <= 0:
        return 0.0

    current_kwh = calculate_charge_from_percentage(current_soc, capacity_kwh)
    target_kwh = calculate_charge_from_percentage(target_soc, capacity_kwh)

    return target_kwh - current_kwh


def is_battery_full(soc_percent: float, tolerance: float = 0.1) -> bool:
    """
    Check if battery is considered full within tolerance.

    Args:
        soc_percent: State of charge percentage (0-100)
        tolerance: Tolerance for "full" determination (default 0.1%)

    Returns:
        True if battery is considered full

    Examples:
        >>> is_battery_full(100.0)
        True
        >>> is_battery_full(99.95)
        True
        >>> is_battery_full(99.5)
        False
    """
    return soc_percent >= (100.0 - tolerance)


def is_battery_empty(soc_percent: float, tolerance: float = 0.1) -> bool:
    """
    Check if battery is considered empty within tolerance.

    Args:
        soc_percent: State of charge percentage (0-100)
        tolerance: Tolerance for "empty" determination (default 0.1%)

    Returns:
        True if battery is considered empty

    Examples:
        >>> is_battery_empty(0.0)
        True
        >>> is_battery_empty(0.05)
        True
        >>> is_battery_empty(0.5)
        False
    """
    return soc_percent <= tolerance


def clamp_soc_to_limits(soc_percent: float, min_soc: float = 0.0, max_soc: float = 100.0) -> float:
    """
    Clamp SOC percentage to specified limits.

    Args:
        soc_percent: State of charge percentage
        min_soc: Minimum allowed SOC (default 0.0)
        max_soc: Maximum allowed SOC (default 100.0)

    Returns:
        Clamped SOC percentage

    Examples:
        >>> clamp_soc_to_limits(105.0)
        100.0
        >>> clamp_soc_to_limits(-5.0)
        0.0
        >>> clamp_soc_to_limits(50.0, 10.0, 90.0)
        50.0
    """
    return max(min_soc, min(max_soc, soc_percent))


def calculate_cycle_equivalent(energy_kwh: float, capacity_kwh: float) -> float:
    """
    Calculate battery cycle equivalent from energy throughput.

    Args:
        energy_kwh: Energy throughput in kWh
        capacity_kwh: Battery capacity in kWh

    Returns:
        Cycle equivalent (1.0 = one full cycle)

    Examples:
        >>> calculate_cycle_equivalent(10.0, 10.0)
        1.0
        >>> calculate_cycle_equivalent(5.0, 10.0)
        0.5
    """
    if capacity_kwh <= 0:
        return 0.0
    return energy_kwh / capacity_kwh


# Backward compatibility functions - maintain existing interfaces
def calc_percent_limit_single(charge_limit: float, soc_max: float) -> int:
    """
    Backward compatibility wrapper for single value percentage calculation.
    Maintains exact behavior of original calc_percent_limit function.
    """
    if soc_max <= 0:
        return 0
    return min(int((float(charge_limit) / soc_max * 100.0) + 0.5), 100)


def calc_percent_limit_list(charge_limit: List[float], soc_max: float) -> List[int]:
    """
    Backward compatibility wrapper for list percentage calculation.
    Maintains exact behavior of original calc_percent_limit function.
    """
    if soc_max <= 0:
        return [0 for i in range(len(charge_limit))]
    return [min(int((float(charge_limit[i]) / soc_max * 100.0) + 0.5), 100) for i in range(len(charge_limit))]


def calc_percent_limit_new(charge_limit: Union[float, List[float]], soc_max: float) -> Union[int, List[int]]:
    """
    Enhanced version of calc_percent_limit with better type hints and documentation.
    Maintains 100% backward compatibility with existing function.

    Args:
        charge_limit: Charge limit(s) in kWh
        soc_max: Maximum SOC capacity in kWh

    Returns:
        Percentage(s) as integer(s), rounded to nearest whole percent
    """
    if isinstance(charge_limit, list):
        return calc_percent_limit_list(charge_limit, soc_max)
    else:
        return calc_percent_limit_single(charge_limit, soc_max)
