# -----------------------------------------------------------------------------
# Predbat Home Battery System - Window Manager Class
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Object-oriented window management class following SOLID principles.

This class handles charging and discharging window operations including
overlap detection, window filtering, and conflict resolution.
"""

from typing import List, Dict, Any, Tuple
from dataclasses import dataclass
from enum import Enum


class WindowType(Enum):
    """Enumeration for different window types."""

    CHARGE = "charge"
    DISCHARGE = "discharge"
    EXPORT = "export"


@dataclass
class Window:
    """Represents a time window for battery operations."""

    start: int
    end: int
    average: float
    window_type: WindowType

    def __post_init__(self):
        """Validate window parameters."""
        if self.end <= self.start:
            raise ValueError(f"Window end ({self.end}) must be after start ({self.start})")
        if self.start < 0:
            raise ValueError(f"Window start ({self.start}) cannot be negative")

    @property
    def duration(self) -> int:
        """Get window duration in minutes."""
        return self.end - self.start

    def overlaps_with(self, other: "Window") -> bool:
        """Check if this window overlaps with another window."""
        return self.start < other.end and other.start < self.end

    def contains_time(self, time: int) -> bool:
        """Check if a specific time falls within this window."""
        return self.start <= time < self.end


class WindowManager:
    """
    Manages battery charge and discharge windows.

    This class follows SOLID principles by:
    - Single Responsibility: Only handles window operations
    - Open/Closed: Extensible for new window types
    - Liskov Substitution: Can be replaced by subclasses
    - Interface Segregation: Clean, focused interface
    - Dependency Inversion: Depends on abstractions (Window class)
    """

    def __init__(self, min_window_duration: int = 5):
        """
        Initialize window manager.

        Args:
            min_window_duration: Minimum duration for a valid window in minutes
        """
        self._min_duration = min_window_duration

    @property
    def min_duration(self) -> int:
        """Get minimum window duration."""
        return self._min_duration

    def set_min_duration(self, duration: int) -> None:
        """Set minimum window duration."""
        if duration < 0:
            raise ValueError("Minimum duration cannot be negative")
        self._min_duration = duration

    def create_window(self, start: int, end: int, average: float, window_type: WindowType) -> Window:
        """
        Create a new window with validation.

        Args:
            start: Start time in minutes
            end: End time in minutes
            average: Average rate or price for the window
            window_type: Type of window (charge, discharge, export)

        Returns:
            Validated Window object

        Raises:
            ValueError: If window parameters are invalid
        """
        window = Window(start, end, average, window_type)

        if window.duration < self._min_duration:
            raise ValueError(f"Window duration ({window.duration}) is less than minimum ({self._min_duration})")

        return window

    def remove_intersecting_windows(self, charge_limits: List[float], charge_windows: List[Dict[str, Any]], export_limits: List[float], export_windows: List[Dict[str, Any]]) -> Tuple[List[float], List[Dict[str, Any]]]:
        """
        Remove or clip charge windows that intersect with discharge windows.

        This is the legacy interface maintained for backward compatibility.
        Uses the original algorithm from utils.py.

        Args:
            charge_limits: List of charge limits for each window
            charge_windows: List of charge window dictionaries
            export_limits: List of export limits for each window
            export_windows: List of export window dictionaries

        Returns:
            Tuple of (filtered_charge_limits, filtered_charge_windows)
        """
        clip_again = True

        # For each charge window
        while clip_again:
            clip_again = False
            new_limit_best = []
            new_window_best = []
            for window_n in range(len(charge_limits)):
                window = charge_windows[window_n]
                start = window["start"]
                end = window["end"]
                average = window["average"]
                limit = charge_limits[window_n]
                clipped = False

                # For each discharge window
                for dwindow_n in range(len(export_limits)):
                    dwindow = export_windows[dwindow_n]
                    dlimit = export_limits[dwindow_n]
                    dstart = dwindow["start"]
                    dend = dwindow["end"]

                    # Overlapping window with enabled discharge?
                    if (limit > 0.0) and (dlimit < 100.0) and (dstart < end) and (dend >= start):
                        if dstart <= start:
                            if start != dend:
                                start = dend
                                clipped = True
                        elif dend >= end:
                            if end != dstart:
                                end = dstart
                                clipped = True
                        else:
                            # Two segments
                            if (dstart - start) >= self._min_duration:
                                new_window = {}
                                new_window["start"] = start
                                new_window["end"] = dstart
                                new_window["average"] = average
                                new_window_best.append(new_window)
                                new_limit_best.append(limit)
                            start = dend
                            clipped = True
                            if (end - start) >= self._min_duration:
                                clip_again = True

                if not clipped or ((end - start) >= self._min_duration):
                    new_window = {}
                    new_window["start"] = start
                    new_window["end"] = end
                    new_window["average"] = average
                    new_window_best.append(new_window)
                    new_limit_best.append(limit)

            if clip_again:
                charge_windows = new_window_best.copy()
                charge_limits = new_limit_best.copy()

        return new_limit_best, new_window_best

    def resolve_window_conflicts(self, charge_windows: List[Window], discharge_windows: List[Window], charge_enabled_threshold: float = 0.0, discharge_enabled_threshold: float = 100.0) -> List[Window]:
        """
        Modern OO interface for resolving window conflicts.

        Args:
            charge_windows: List of charge windows
            discharge_windows: List of discharge windows
            charge_enabled_threshold: Threshold above which charging is enabled
            discharge_enabled_threshold: Threshold below which discharging is enabled

        Returns:
            List of non-conflicting charge windows
        """
        resolved_windows = []

        for charge_window in charge_windows:
            current_start = charge_window.start
            current_end = charge_window.end

            # Check against all discharge windows
            for discharge_window in discharge_windows:
                if charge_window.overlaps_with(discharge_window):
                    # Handle overlap - clip or split the charge window
                    if discharge_window.start <= current_start < discharge_window.end:
                        # Discharge starts before or at charge start
                        if current_end > discharge_window.end:
                            current_start = discharge_window.end
                        else:
                            # Entire charge window is covered
                            current_start = current_end
                            break
                    elif current_start < discharge_window.start < current_end:
                        # Discharge starts within charge window
                        if discharge_window.end >= current_end:
                            # Discharge extends to or beyond charge end
                            current_end = discharge_window.start
                        else:
                            # Discharge is completely within charge window - split
                            if discharge_window.start - current_start >= self._min_duration:
                                # First segment is valid
                                first_window = Window(current_start, discharge_window.start, charge_window.average, WindowType.CHARGE)
                                resolved_windows.append(first_window)

                            # Continue with second segment
                            current_start = discharge_window.end

            # Add remaining window if it's valid
            if current_end - current_start >= self._min_duration:
                remaining_window = Window(current_start, current_end, charge_window.average, WindowType.CHARGE)
                resolved_windows.append(remaining_window)

        return resolved_windows

    def find_optimal_windows(self, available_windows: List[Window], required_duration: int, max_windows: int = 3) -> List[Window]:
        """
        Find optimal windows for battery operations.

        Args:
            available_windows: List of available windows
            required_duration: Total duration needed in minutes
            max_windows: Maximum number of windows to select

        Returns:
            List of optimal windows sorted by average rate/price
        """
        # Sort windows by average rate (assuming lower is better for charging)
        sorted_windows = sorted(available_windows, key=lambda w: w.average)

        selected_windows = []
        total_duration = 0

        for window in sorted_windows:
            if len(selected_windows) >= max_windows:
                break

            if total_duration >= required_duration:
                break

            # Check if this window conflicts with already selected windows
            conflicts = any(window.overlaps_with(selected) for selected in selected_windows)

            if not conflicts:
                selected_windows.append(window)
                total_duration += window.duration

        return selected_windows

    def merge_adjacent_windows(self, windows: List[Window], tolerance: int = 0) -> List[Window]:
        """
        Merge adjacent or nearly adjacent windows.

        Args:
            windows: List of windows to merge
            tolerance: Maximum gap between windows to still merge (minutes)

        Returns:
            List of merged windows
        """
        if not windows:
            return []

        # Sort windows by start time
        sorted_windows = sorted(windows, key=lambda w: w.start)
        merged = [sorted_windows[0]]

        for current in sorted_windows[1:]:
            last_merged = merged[-1]

            # Check if windows can be merged
            gap = current.start - last_merged.end
            same_type = current.window_type == last_merged.window_type

            if gap <= tolerance and same_type:
                # Merge windows - use weighted average for rate
                total_duration = last_merged.duration + current.duration
                weighted_average = (last_merged.average * last_merged.duration + current.average * current.duration) / total_duration

                merged_window = Window(last_merged.start, current.end, weighted_average, current.window_type)
                merged[-1] = merged_window
            else:
                merged.append(current)

        return merged

    def validate_windows(self, windows: List[Window]) -> List[str]:
        """
        Validate a list of windows and return any errors found.

        Args:
            windows: List of windows to validate

        Returns:
            List of error messages (empty if all valid)
        """
        errors = []

        for i, window in enumerate(windows):
            if window.duration < self._min_duration:
                errors.append(f"Window {i}: Duration {window.duration} < minimum {self._min_duration}")

            if window.start < 0:
                errors.append(f"Window {i}: Start time {window.start} cannot be negative")

            if window.end <= window.start:
                errors.append(f"Window {i}: End time {window.end} must be after start {window.start}")

        # Check for overlaps within same type
        for i, window1 in enumerate(windows):
            for j, window2 in enumerate(windows[i + 1 :], i + 1):
                if window1.window_type == window2.window_type and window1.overlaps_with(window2):
                    errors.append(f"Windows {i} and {j}: Same-type windows overlap")

        return errors
