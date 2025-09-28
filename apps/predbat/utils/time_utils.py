# -----------------------------------------------------------------------------
# Predbat Home Battery System - Time Processing Utilities
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Pure time processing functions extracted from utils.py

This module contains stateless, side-effect-free functions for time processing.
All functions are unit tested and maintain backward compatibility.
"""

from datetime import datetime, timedelta
from typing import Optional


def time_string_to_stamp(time_string: Optional[str]) -> Optional[datetime]:
    """
    Convert a time string to a timestamp.

    Handles HH:MM and HH:MM:SS formats, converting HH:MM to HH:MM:00.
    Returns None for None, "unknown", or invalid inputs.

    Args:
        time_string: Time string in HH:MM or HH:MM:SS format, or None/"unknown"

    Returns:
        datetime object for the time, or None if invalid/unknown

    Examples:
        >>> time_string_to_stamp("14:30")
        datetime.datetime(1900, 1, 1, 14, 30)
        >>> time_string_to_stamp("14:30:45")
        datetime.datetime(1900, 1, 1, 14, 30, 45)
        >>> time_string_to_stamp(None)
        None
        >>> time_string_to_stamp("unknown")
        None
    """
    if time_string is None:
        return None
    if time_string == "unknown":
        return None

    if isinstance(time_string, str) and len(time_string) == 5:
        time_string += ":00"

    return datetime.strptime(time_string, "%H:%M:%S")


def minutes_since_yesterday(now: datetime) -> int:
    """
    Calculate the number of minutes since 23:59 yesterday.

    This is commonly used for calculating time offsets in battery scheduling.

    Args:
        now: Current datetime

    Returns:
        Integer number of minutes since yesterday at 23:59

    Examples:
        >>> from datetime import datetime
        >>> now = datetime(2024, 1, 15, 10, 30)  # 10:30 AM
        >>> minutes_since_yesterday(now)
        631  # 10 hours 31 minutes = 631 minutes
    """
    yesterday = now - timedelta(days=1)
    yesterday_at_2359 = datetime.combine(yesterday, datetime.max.time())
    difference = now - yesterday_at_2359
    difference_minutes = int((difference.seconds + 59) / 60)
    return difference_minutes


def minutes_to_time(updated: datetime, now: datetime) -> int:
    """
    Compute the number of minutes between a time (now) and the updated time.

    Handles both positive differences (future) and negative (past), as well
    as multi-day differences.

    Args:
        updated: Target datetime
        now: Reference datetime (typically current time)

    Returns:
        Integer number of minutes between the two times

    Examples:
        >>> from datetime import datetime
        >>> now = datetime(2024, 1, 15, 10, 0)
        >>> updated = datetime(2024, 1, 15, 12, 30)
        >>> minutes_to_time(updated, now)
        150  # 2.5 hours = 150 minutes
    """
    timeday = updated - now
    minutes = int(timeday.seconds / 60) + int(timeday.days * 60 * 24)
    return minutes


def str2time(time_str: str, time_format_seconds: str = "%Y-%m-%dT%H:%M:%S.%f%z", time_format: str = "%Y-%m-%dT%H:%M:%S%z", time_format_octopus: str = "%Y-%m-%d %H:%M:%S%z") -> datetime:
    """
    Parse various time string formats into datetime objects.

    Supports three common formats:
    1. With fractional seconds (contains ".")
    2. ISO format with T separator (contains "T")
    3. Octopus format with space separator

    Args:
        time_str: Time string to parse
        time_format_seconds: Format for strings with fractional seconds
        time_format: Format for ISO strings with T separator
        time_format_octopus: Format for Octopus-style strings

    Returns:
        Parsed datetime object

    Examples:
        >>> str2time("2024-01-15T14:30:45.123456+0000")  # With fractional seconds
        >>> str2time("2024-01-15T14:30:45+0000")  # ISO format
        >>> str2time("2024-01-15 14:30:45+0000")  # Octopus format
    """
    if "." in time_str:
        tdata = datetime.strptime(time_str, time_format_seconds)
    elif "T" in time_str:
        tdata = datetime.strptime(time_str, time_format)
    else:
        tdata = datetime.strptime(time_str, time_format_octopus)
    return tdata
