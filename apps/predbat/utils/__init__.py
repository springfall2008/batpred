# -----------------------------------------------------------------------------
# Predbat Home Battery System - Utils Package
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Utilities package for PredBat battery calculations and helper functions.

This package provides both modern object-oriented classes and backward-compatible
functional interfaces. The OO classes follow SOLID principles and are easily
testable, while the functional interface maintains compatibility with existing code.

## Object-Oriented Interface (Recommended)

For new code, use the modern OO classes:

```python
from utils import BatteryManager, WindowManager, TimeManager, FormattingManager

# Dependency injection with configuration
battery = BatteryManager(BatteryConfig(minute_watt=60000))
time_mgr = TimeManager()
formatter = FormattingManager()

# Clean, testable methods
soc_percent = battery.calculate_soc_percentage(5.0, 10.0)
time_stamp = time_mgr.time_string_to_stamp("14:30")
formatted = formatter.format_power(3000)
```

## Functional Interface (Legacy Compatibility)

For existing code, all original functions are still available:

```python
from utils import calc_percent_limit, time_string_to_stamp, dp2

# Original functional interface still works
percent = calc_percent_limit(5.0, 10.0)
timestamp = time_string_to_stamp("14:30")
rounded = dp2(3.14159)
```
"""

# Modern OO Classes (Recommended for new code)
from .battery_manager import BatteryManager, BatteryConfig
from .window_manager import WindowManager, Window, WindowType
from .time_manager import TimeManager, TimeFormats, TimeFormat
from .formatting_manager import FormattingManager, FormattingConfig, Unit

# Legacy functional interface - import all public functions to maintain backward compatibility
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
    calc_percent_limit,
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

from .window_operations import (
    remove_intersecting_windows,
)


# Backward compatibility wrappers that use the default BatteryManager
def get_charge_rate_curve(*args, **kwargs):
    """Legacy wrapper - use BatteryManager.get_charge_rate_curve() for new code."""
    return get_default_battery_manager().get_charge_rate_curve(*args, **kwargs)


def get_discharge_rate_curve(*args, **kwargs):
    """Legacy wrapper - use BatteryManager.get_discharge_rate_curve() for new code."""
    return get_default_battery_manager().get_discharge_rate_curve(*args, **kwargs)


def find_battery_temperature_cap(*args, **kwargs):
    """Legacy wrapper - use BatteryManager.find_battery_temperature_cap() for new code."""
    return get_default_battery_manager().find_battery_temperature_cap(*args, **kwargs)


def find_charge_rate(*args, **kwargs):
    """Legacy wrapper - use BatteryManager.find_charge_rate() for new code."""
    return get_default_battery_manager().find_charge_rate(*args, **kwargs)


# Create default instances for convenient functional interface
_default_battery_manager = None
_default_time_manager = None
_default_formatting_manager = None
_default_window_manager = None


def get_default_battery_manager() -> BatteryManager:
    """Get default BatteryManager instance (singleton pattern)."""
    global _default_battery_manager
    if _default_battery_manager is None:
        _default_battery_manager = BatteryManager()
    return _default_battery_manager


def get_default_time_manager() -> TimeManager:
    """Get default TimeManager instance (singleton pattern)."""
    global _default_time_manager
    if _default_time_manager is None:
        _default_time_manager = TimeManager()
    return _default_time_manager


def get_default_formatting_manager() -> FormattingManager:
    """Get default FormattingManager instance (singleton pattern)."""
    global _default_formatting_manager
    if _default_formatting_manager is None:
        _default_formatting_manager = FormattingManager()
    return _default_formatting_manager


def get_default_window_manager() -> WindowManager:
    """Get default WindowManager instance (singleton pattern)."""
    global _default_window_manager
    if _default_window_manager is None:
        _default_window_manager = WindowManager()
    return _default_window_manager


__all__ = [
    # Modern OO Classes (Recommended)
    "BatteryManager",
    "BatteryConfig",
    "WindowManager",
    "Window",
    "WindowType",
    "TimeManager",
    "TimeFormats",
    "TimeFormat",
    "FormattingManager",
    "FormattingConfig",
    "Unit",
    # Default instance getters
    "get_default_battery_manager",
    "get_default_time_manager",
    "get_default_formatting_manager",
    "get_default_window_manager",
    # Legacy functional interface
    # Battery calculations
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
    "calc_percent_limit",
    # Formatting
    "dp0",
    "dp1",
    "dp2",
    "dp3",
    "dp4",
    # Time utilities
    "time_string_to_stamp",
    "minutes_since_yesterday",
    "minutes_to_time",
    "str2time",
    # Window operations
    "remove_intersecting_windows",
    # Battery curves (legacy wrappers)
    "get_charge_rate_curve",
    "get_discharge_rate_curve",
    "find_battery_temperature_cap",
    # Charge calculations (legacy wrapper)
    "find_charge_rate",
]
