# -----------------------------------------------------------------------------
# Predbat Home Battery System - Utils Package
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Utilities package for PredBat battery calculations and helper functions.

This package contains pure, stateless utility functions that can be easily
unit tested and reused across the application.
"""

# Import all public functions to maintain backward compatibility
from .battery_calculations import (
    calculate_soc_percentage,
    calculate_charge_from_percentage,
    calculate_charge_time_minutes,
    calculate_discharge_time_minutes,
    apply_efficiency_loss,
    calculate_energy_needed_for_soc,
    is_battery_full,
    is_battery_empty,
    clamp_soc_to_limits,
    calculate_cycle_equivalent,
    calc_percent_limit_new,
)

from .formatting import (
    dp0,
    dp1,
    dp2,
    dp3,
    dp4,
)

from .time_utils import (
    time_string_to_stamp,
    minutes_since_yesterday,
    minutes_to_time,
    str2time,
)

__all__ = [
    "calculate_soc_percentage",
    "calculate_charge_from_percentage",
    "calculate_charge_time_minutes",
    "calculate_discharge_time_minutes",
    "apply_efficiency_loss",
    "calculate_energy_needed_for_soc",
    "is_battery_full",
    "is_battery_empty",
    "clamp_soc_to_limits",
    "calculate_cycle_equivalent",
    "calc_percent_limit_new",
    "dp0",
    "dp1",
    "dp2",
    "dp3",
    "dp4",
    "time_string_to_stamp",
    "minutes_since_yesterday",
    "minutes_to_time",
    "str2time",
]
