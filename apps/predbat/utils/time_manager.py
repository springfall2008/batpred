# -----------------------------------------------------------------------------
# Predbat Home Battery System - Time Manager Class
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Object-oriented time management class following SOLID principles.

This class handles all time-related operations including parsing, formatting,
calculations, and timezone handling for battery scheduling.
"""

from datetime import datetime, timedelta
from typing import Optional, Union, List
from dataclasses import dataclass
from enum import Enum
import re


class TimeFormat(Enum):
    """Enumeration for supported time formats."""

    ISO_WITH_TIMEZONE = "%Y-%m-%dT%H:%M:%S%z"
    ISO_WITH_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
    OCTOPUS = "%Y-%m-%d %H:%M:%S%z"
    SOLIS = "%Y-%m-%d %H:%M:%S"
    FORECAST_SOLAR = "%Y-%m-%d %H:%M:%S"
    SOLCAST = "%Y-%m-%dT%H:%M:%S.%f0%z"
    TIME_ONLY = "%H:%M:%S"


@dataclass
class TimeFormats:
    """Configuration for time format strings."""

    iso_with_timezone: str = "%Y-%m-%dT%H:%M:%S%z"
    iso_with_seconds: str = "%Y-%m-%dT%H:%M:%S.%f%z"
    octopus: str = "%Y-%m-%d %H:%M:%S%z"
    solis: str = "%Y-%m-%d %H:%M:%S"
    forecast_solar: str = "%Y-%m-%d %H:%M:%S"
    solcast: str = "%Y-%m-%dT%H:%M:%S.%f0%z"
    time_only: str = "%H:%M:%S"


class TimeManager:
    """
    Manages all time-related operations and calculations.

    This class follows SOLID principles by:
    - Single Responsibility: Only handles time operations
    - Open/Closed: Extensible for new time formats and operations
    - Liskov Substitution: Can be replaced by subclasses
    - Interface Segregation: Clean, focused interface for time operations
    - Dependency Inversion: Depends on abstractions (TimeFormats config)
    """

    def __init__(self, formats: Optional[TimeFormats] = None):
        """
        Initialize time manager with format configuration.

        Args:
            formats: Time format configuration. Uses defaults if None.
        """
        self._formats = formats or TimeFormats()

    @property
    def formats(self) -> TimeFormats:
        """Get current time format configuration."""
        return self._formats

    def update_formats(self, **kwargs) -> None:
        """Update time format configuration."""
        for key, value in kwargs.items():
            if hasattr(self._formats, key):
                setattr(self._formats, key, value)
            else:
                raise ValueError(f"Unknown format parameter: {key}")

    # Time String Parsing
    def time_string_to_stamp(self, time_string: Optional[str]) -> Optional[datetime]:
        """
        Convert a time string to a timestamp.

        Handles HH:MM and HH:MM:SS formats, converting HH:MM to HH:MM:00.
        Returns None for None, "unknown", or invalid inputs.

        Args:
            time_string: Time string in HH:MM or HH:MM:SS format

        Returns:
            datetime object for the time, or None if invalid

        Examples:
            >>> tm = TimeManager()
            >>> tm.time_string_to_stamp("14:30")
            datetime.datetime(1900, 1, 1, 14, 30)
            >>> tm.time_string_to_stamp("14:30:45")
            datetime.datetime(1900, 1, 1, 14, 30, 45)
            >>> tm.time_string_to_stamp(None)
            None
        """
        if time_string is None:
            return None
        if time_string == "unknown":
            return None

        if isinstance(time_string, str) and len(time_string) == 5:
            time_string += ":00"

        try:
            return datetime.strptime(time_string, self._formats.time_only)
        except ValueError as e:
            raise ValueError(f"Invalid time string format: {time_string}") from e

    def str2time(self, time_str: str, auto_detect: bool = True, format_hint: Optional[TimeFormat] = None) -> datetime:
        """
        Parse various time string formats into datetime objects.

        Args:
            time_str: Time string to parse
            auto_detect: Whether to auto-detect format (default True)
            format_hint: Specific format to try first

        Returns:
            Parsed datetime object

        Raises:
            ValueError: If string cannot be parsed with any known format

        Examples:
            >>> tm = TimeManager()
            >>> tm.str2time("2024-01-15T14:30:45.123456+0000")  # ISO with seconds
            >>> tm.str2time("2024-01-15T14:30:45+0000")  # ISO format
            >>> tm.str2time("2024-01-15 14:30:45+0000")  # Octopus format
        """
        if not auto_detect and format_hint:
            # Try specific format first
            format_str = getattr(self._formats, format_hint.value.split("_")[-1].lower())
            try:
                return datetime.strptime(time_str, format_str)
            except ValueError:
                pass

        # Auto-detection logic (original algorithm)
        if "." in time_str:
            try:
                return datetime.strptime(time_str, self._formats.iso_with_seconds)
            except ValueError:
                try:
                    return datetime.strptime(time_str, self._formats.solcast)
                except ValueError:
                    pass
        elif "T" in time_str:
            try:
                return datetime.strptime(time_str, self._formats.iso_with_timezone)
            except ValueError:
                pass
        else:
            # Try various formats without T separator
            formats_to_try = [
                self._formats.octopus,
                self._formats.solis,
                self._formats.forecast_solar,
            ]

            for fmt in formats_to_try:
                try:
                    return datetime.strptime(time_str, fmt)
                except ValueError:
                    continue

        raise ValueError(f"Unable to parse time string: {time_str}")

    def parse_time_flexible(self, time_input: Union[str, datetime, int, float]) -> datetime:
        """
        Flexibly parse time input from various types.

        Args:
            time_input: Time as string, datetime, or timestamp

        Returns:
            Parsed datetime object
        """
        if isinstance(time_input, datetime):
            return time_input
        elif isinstance(time_input, (int, float)):
            return datetime.fromtimestamp(time_input)
        elif isinstance(time_input, str):
            return self.str2time(time_input)
        else:
            raise TypeError(f"Unsupported time input type: {type(time_input)}")

    # Time Calculations
    def minutes_since_yesterday(self, now: datetime) -> int:
        """
        Calculate the number of minutes since 23:59 yesterday.

        Args:
            now: Current datetime

        Returns:
            Integer number of minutes since yesterday at 23:59

        Examples:
            >>> tm = TimeManager()
            >>> now = datetime(2024, 1, 15, 10, 30)  # 10:30 AM
            >>> tm.minutes_since_yesterday(now)
            631  # 10 hours 31 minutes = 631 minutes
        """
        yesterday = now - timedelta(days=1)
        yesterday_at_2359 = datetime.combine(yesterday, datetime.max.time())
        difference = now - yesterday_at_2359
        difference_minutes = int((difference.seconds + 59) / 60)
        return difference_minutes

    def minutes_to_time(self, updated: datetime, now: datetime) -> int:
        """
        Compute the number of minutes between two datetimes.

        Args:
            updated: Target datetime
            now: Reference datetime (typically current time)

        Returns:
            Integer number of minutes between the two times

        Examples:
            >>> tm = TimeManager()
            >>> now = datetime(2024, 1, 15, 10, 0)
            >>> updated = datetime(2024, 1, 15, 12, 30)
            >>> tm.minutes_to_time(updated, now)
            150  # 2.5 hours = 150 minutes
        """
        timeday = updated - now
        minutes = int(timeday.seconds / 60) + int(timeday.days * 60 * 24)
        return minutes

    def time_difference_minutes(self, start: datetime, end: datetime) -> int:
        """
        Calculate difference between two times in minutes.

        Args:
            start: Start datetime
            end: End datetime

        Returns:
            Difference in minutes (positive if end > start)
        """
        diff = end - start
        return int(diff.total_seconds() / 60)

    def add_minutes(self, base_time: datetime, minutes: int) -> datetime:
        """
        Add minutes to a datetime.

        Args:
            base_time: Base datetime
            minutes: Minutes to add (can be negative)

        Returns:
            New datetime with minutes added
        """
        return base_time + timedelta(minutes=minutes)

    def round_to_nearest_minute(self, dt: datetime) -> datetime:
        """
        Round datetime to the nearest minute.

        Args:
            dt: Datetime to round

        Returns:
            Rounded datetime
        """
        if dt.second >= 30:
            return dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        else:
            return dt.replace(second=0, microsecond=0)

    # Time Range Operations
    def is_time_in_range(self, time: datetime, start: datetime, end: datetime) -> bool:
        """
        Check if a time falls within a range.

        Args:
            time: Time to check
            start: Range start (inclusive)
            end: Range end (exclusive)

        Returns:
            True if time is in range
        """
        return start <= time < end

    def generate_time_range(self, start: datetime, end: datetime, step_minutes: int = 60) -> List[datetime]:
        """
        Generate a range of datetime objects.

        Args:
            start: Start datetime
            end: End datetime
            step_minutes: Step size in minutes

        Returns:
            List of datetime objects in range
        """
        times = []
        current = start
        while current < end:
            times.append(current)
            current += timedelta(minutes=step_minutes)
        return times

    def get_day_boundaries(self, dt: datetime) -> tuple[datetime, datetime]:
        """
        Get start and end of day for a given datetime.

        Args:
            dt: Input datetime

        Returns:
            Tuple of (day_start, day_end)
        """
        day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        return day_start, day_end

    # Time Validation
    def validate_time_string(self, time_str: str) -> bool:
        """
        Validate if a string can be parsed as a time.

        Args:
            time_str: String to validate

        Returns:
            True if string can be parsed
        """
        try:
            self.str2time(time_str)
            return True
        except ValueError:
            return False

    def validate_time_range(self, start: datetime, end: datetime) -> bool:
        """
        Validate that a time range is valid.

        Args:
            start: Range start
            end: Range end

        Returns:
            True if range is valid (end > start)
        """
        return end > start

    # Formatting
    def format_time(self, dt: datetime, format_type: TimeFormat = TimeFormat.ISO_WITH_TIMEZONE) -> str:
        """
        Format datetime to string using specified format.

        Args:
            dt: Datetime to format
            format_type: Format to use

        Returns:
            Formatted time string
        """
        format_map = {
            TimeFormat.ISO_WITH_TIMEZONE: self._formats.iso_with_timezone,
            TimeFormat.ISO_WITH_SECONDS: self._formats.iso_with_seconds,
            TimeFormat.OCTOPUS: self._formats.octopus,
            TimeFormat.SOLIS: self._formats.solis,
            TimeFormat.FORECAST_SOLAR: self._formats.forecast_solar,
            TimeFormat.SOLCAST: self._formats.solcast,
            TimeFormat.TIME_ONLY: self._formats.time_only,
        }

        return dt.strftime(format_map[format_type])

    def format_duration_minutes(self, minutes: int) -> str:
        """
        Format duration in minutes to human-readable string.

        Args:
            minutes: Duration in minutes

        Returns:
            Formatted string (e.g., "2h 30m")
        """
        if minutes < 0:
            return f"-{self.format_duration_minutes(-minutes)}"

        hours = minutes // 60
        mins = minutes % 60

        if hours > 0:
            return f"{hours}h {mins}m" if mins > 0 else f"{hours}h"
        else:
            return f"{mins}m"
