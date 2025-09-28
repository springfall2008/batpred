# -----------------------------------------------------------------------------
# Predbat Home Battery System - Formatting Manager Class
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Object-oriented data formatting class following SOLID principles.

This class handles all data formatting operations including decimal precision,
unit conversions, and display formatting for battery system data.
"""

from typing import Union, List, Dict, Optional
from dataclasses import dataclass
from enum import Enum


class Unit(Enum):
    """Enumeration for different units."""

    WATTS = "W"
    KILOWATTS = "kW"
    WATT_HOURS = "Wh"
    KILOWATT_HOURS = "kWh"
    PERCENTAGE = "%"
    CELSIUS = "°C"
    FAHRENHEIT = "°F"
    MINUTES = "min"
    HOURS = "h"
    POUNDS = "£"
    EUROS = "€"
    DOLLARS = "$"


@dataclass
class FormattingConfig:
    """Configuration for data formatting."""

    default_decimal_places: int = 2
    currency_decimal_places: int = 2
    percentage_decimal_places: int = 1
    energy_decimal_places: int = 2
    power_decimal_places: int = 1
    temperature_decimal_places: int = 1

    # Unit conversion factors
    watt_to_kilowatt: float = 1000.0
    minute_to_hour: float = 60.0

    # Display preferences
    use_thousands_separator: bool = True
    thousands_separator: str = ","
    decimal_separator: str = "."


class FormattingManager:
    """
    Manages all data formatting operations.

    This class follows SOLID principles by:
    - Single Responsibility: Only handles data formatting
    - Open/Closed: Extensible for new formatting rules
    - Liskov Substitution: Can be replaced by subclasses
    - Interface Segregation: Clean, focused interface
    - Dependency Inversion: Depends on FormattingConfig abstraction
    """

    def __init__(self, config: Optional[FormattingConfig] = None):
        """
        Initialize formatting manager with configuration.

        Args:
            config: Formatting configuration. Uses defaults if None.
        """
        self._config = config or FormattingConfig()

    @property
    def config(self) -> FormattingConfig:
        """Get current formatting configuration."""
        return self._config

    def update_config(self, **kwargs) -> None:
        """Update formatting configuration."""
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)
            else:
                raise ValueError(f"Unknown config parameter: {key}")

    # Basic Decimal Formatting (Legacy Interface)
    def dp0(self, value: Union[int, float]) -> int:
        """Round to 0 decimal places."""
        return round(value)

    def dp1(self, value: Union[int, float]) -> float:
        """Round to 1 decimal place."""
        return round(value, 1)

    def dp2(self, value: Union[int, float]) -> float:
        """Round to 2 decimal places."""
        return round(value, 2)

    def dp3(self, value: Union[int, float]) -> float:
        """Round to 3 decimal places."""
        return round(value, 3)

    def dp4(self, value: Union[int, float]) -> float:
        """Round to 4 decimal places."""
        return round(value, 4)

    # Modern Formatting Interface
    def format_decimal(self, value: Union[int, float], decimal_places: Optional[int] = None, use_thousands_separator: Optional[bool] = None) -> str:
        """
        Format a number with specified decimal places and optional thousands separator.

        Args:
            value: Number to format
            decimal_places: Number of decimal places (uses config default if None)
            use_thousands_separator: Whether to use thousands separator

        Returns:
            Formatted number string
        """
        if decimal_places is None:
            decimal_places = self._config.default_decimal_places

        if use_thousands_separator is None:
            use_thousands_separator = self._config.use_thousands_separator

        # Round to specified decimal places
        rounded_value = round(value, decimal_places)

        # Format with decimal places
        if decimal_places == 0:
            formatted = f"{int(rounded_value)}"
        else:
            formatted = f"{rounded_value:.{decimal_places}f}"

        # Add thousands separator if requested
        if use_thousands_separator and abs(rounded_value) >= 1000:
            parts = formatted.split(".")
            integer_part = parts[0]
            decimal_part = parts[1] if len(parts) > 1 else ""

            # Add thousands separators to integer part
            if integer_part.startswith("-"):
                sign = "-"
                integer_part = integer_part[1:]
            else:
                sign = ""

            # Insert separators every 3 digits from right
            separated = []
            for i, digit in enumerate(reversed(integer_part)):
                if i > 0 and i % 3 == 0:
                    separated.append(self._config.thousands_separator)
                separated.append(digit)

            integer_part = sign + "".join(reversed(separated))
            formatted = integer_part + (f".{decimal_part}" if decimal_part else "")

        return formatted

    # Unit-Specific Formatting
    def format_power(self, watts: float, unit: Unit = Unit.KILOWATTS) -> str:
        """
        Format power values with appropriate units.

        Args:
            watts: Power in watts
            unit: Target unit for display

        Returns:
            Formatted power string with unit
        """
        if unit == Unit.KILOWATTS:
            value = watts / self._config.watt_to_kilowatt
            formatted = self.format_decimal(value, self._config.power_decimal_places)
            return f"{formatted} {unit.value}"
        elif unit == Unit.WATTS:
            formatted = self.format_decimal(watts, 0)
            return f"{formatted} {unit.value}"
        else:
            raise ValueError(f"Unsupported power unit: {unit}")

    def format_energy(self, watt_hours: float, unit: Unit = Unit.KILOWATT_HOURS) -> str:
        """
        Format energy values with appropriate units.

        Args:
            watt_hours: Energy in watt-hours
            unit: Target unit for display

        Returns:
            Formatted energy string with unit
        """
        if unit == Unit.KILOWATT_HOURS:
            value = watt_hours / self._config.watt_to_kilowatt
            formatted = self.format_decimal(value, self._config.energy_decimal_places)
            return f"{formatted} {unit.value}"
        elif unit == Unit.WATT_HOURS:
            formatted = self.format_decimal(watt_hours, 0)
            return f"{formatted} {unit.value}"
        else:
            raise ValueError(f"Unsupported energy unit: {unit}")

    def format_percentage(self, value: float, show_symbol: bool = True) -> str:
        """
        Format percentage values.

        Args:
            value: Percentage value (0-100)
            show_symbol: Whether to include % symbol

        Returns:
            Formatted percentage string
        """
        formatted = self.format_decimal(value, self._config.percentage_decimal_places)
        return f"{formatted}%" if show_symbol else formatted

    def format_currency(self, amount: float, currency: Unit = Unit.POUNDS, show_symbol: bool = True) -> str:
        """
        Format currency values.

        Args:
            amount: Currency amount
            currency: Currency unit
            show_symbol: Whether to include currency symbol

        Returns:
            Formatted currency string
        """
        formatted = self.format_decimal(amount, self._config.currency_decimal_places)

        if show_symbol:
            if currency in [Unit.POUNDS, Unit.EUROS]:
                return f"{currency.value}{formatted}"
            else:  # Dollars and others
                return f"{currency.value}{formatted}"
        else:
            return formatted

    def format_temperature(self, celsius: float, target_unit: Unit = Unit.CELSIUS) -> str:
        """
        Format temperature values with unit conversion.

        Args:
            celsius: Temperature in Celsius
            target_unit: Target temperature unit

        Returns:
            Formatted temperature string with unit
        """
        if target_unit == Unit.CELSIUS:
            value = celsius
        elif target_unit == Unit.FAHRENHEIT:
            value = celsius * 9 / 5 + 32
        else:
            raise ValueError(f"Unsupported temperature unit: {target_unit}")

        formatted = self.format_decimal(value, self._config.temperature_decimal_places)
        return f"{formatted}{target_unit.value}"

    def format_duration(self, minutes: int, show_units: bool = True) -> str:
        """
        Format duration in minutes to human-readable string.

        Args:
            minutes: Duration in minutes
            show_units: Whether to show unit labels

        Returns:
            Formatted duration string
        """
        if minutes < 0:
            return f"-{self.format_duration(-minutes, show_units)}"

        hours = minutes // int(self._config.minute_to_hour)
        mins = minutes % int(self._config.minute_to_hour)

        if show_units:
            if hours > 0:
                return f"{hours}h {mins}m" if mins > 0 else f"{hours}h"
            else:
                return f"{mins}m"
        else:
            if hours > 0:
                return f"{hours}:{mins:02d}"
            else:
                return f"{mins}"

    # List and Dictionary Formatting
    def format_list(self, values: List[Union[int, float]], decimal_places: Optional[int] = None, separator: str = ", ") -> str:
        """
        Format a list of numbers to a string.

        Args:
            values: List of numbers to format
            decimal_places: Decimal places for each number
            separator: Separator between values

        Returns:
            Formatted string representation of the list
        """
        if decimal_places is None:
            decimal_places = self._config.default_decimal_places

        formatted_values = [self.format_decimal(value, decimal_places, use_thousands_separator=False) for value in values]
        return separator.join(formatted_values)

    def format_table_row(self, data: Dict[str, Union[int, float, str]], column_formats: Optional[Dict[str, int]] = None) -> Dict[str, str]:
        """
        Format a dictionary of data for table display.

        Args:
            data: Dictionary of column names to values
            column_formats: Optional decimal places for each column

        Returns:
            Dictionary with formatted string values
        """
        formatted = {}

        for key, value in data.items():
            if isinstance(value, str):
                formatted[key] = value
            elif isinstance(value, (int, float)):
                decimal_places = (column_formats or {}).get(key, self._config.default_decimal_places)
                formatted[key] = self.format_decimal(value, decimal_places)
            else:
                formatted[key] = str(value)

        return formatted

    # Smart Formatting
    def smart_format_value(self, value: Union[int, float], value_type: str = "auto") -> str:
        """
        Intelligently format a value based on its type and magnitude.

        Args:
            value: Value to format
            value_type: Type hint ("power", "energy", "percentage", "currency", "auto")

        Returns:
            Smartly formatted string
        """
        if value_type == "auto":
            # Auto-detect based on value characteristics
            if 0 <= value <= 100 and abs(value - round(value, 1)) < 0.01:
                value_type = "percentage"
            elif abs(value) >= 1000:
                value_type = "energy" if value < 100000 else "power"
            else:
                value_type = "general"

        if value_type == "power":
            return self.format_power(value)
        elif value_type == "energy":
            return self.format_energy(value)
        elif value_type == "percentage":
            return self.format_percentage(value)
        elif value_type == "currency":
            return self.format_currency(value)
        else:
            return self.format_decimal(value)

    # Range and Statistics Formatting
    def format_range(self, min_val: float, max_val: float, decimal_places: Optional[int] = None) -> str:
        """
        Format a range of values.

        Args:
            min_val: Minimum value
            max_val: Maximum value
            decimal_places: Decimal places to use

        Returns:
            Formatted range string (e.g., "10.5 - 15.2")
        """
        min_formatted = self.format_decimal(min_val, decimal_places, use_thousands_separator=False)
        max_formatted = self.format_decimal(max_val, decimal_places, use_thousands_separator=False)
        return f"{min_formatted} - {max_formatted}"

    def format_statistics(self, values: List[float], include_stats: List[str] = ["mean", "min", "max"]) -> Dict[str, str]:
        """
        Format basic statistics for a list of values.

        Args:
            values: List of values to analyze
            include_stats: Which statistics to include

        Returns:
            Dictionary of formatted statistics
        """
        if not values:
            return {stat: "N/A" for stat in include_stats}

        stats = {}

        if "mean" in include_stats:
            stats["mean"] = self.format_decimal(sum(values) / len(values))
        if "min" in include_stats:
            stats["min"] = self.format_decimal(min(values))
        if "max" in include_stats:
            stats["max"] = self.format_decimal(max(values))
        if "median" in include_stats:
            sorted_vals = sorted(values)
            n = len(sorted_vals)
            if n % 2 == 0:
                median = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
            else:
                median = sorted_vals[n // 2]
            stats["median"] = self.format_decimal(median)

        return stats
