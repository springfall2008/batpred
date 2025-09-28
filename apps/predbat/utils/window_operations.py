# -----------------------------------------------------------------------------
# Predbat Home Battery System - Window Operation Utilities
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Window operation functions extracted from utils.py

This module contains functions for handling charging and discharging windows,
including overlap detection and window filtering operations.
"""

from typing import List, Dict, Any, Tuple


def remove_intersecting_windows(charge_limit_best: List[float], charge_window_best: List[Dict[str, Any]], export_limit_best: List[float], export_window_best: List[Dict[str, Any]]) -> Tuple[List[float], List[Dict[str, Any]]]:
    """
    Filters and removes intersecting charge windows.

    This function removes or clips charge windows that overlap with discharge
    (export) windows to prevent conflicting battery operations.

    Args:
        charge_limit_best: List of charge limits for each window
        charge_window_best: List of charge window dictionaries with 'start', 'end', 'average' keys
        export_limit_best: List of export limits for each window
        export_window_best: List of export window dictionaries with 'start', 'end' keys

    Returns:
        Tuple of (filtered_charge_limits, filtered_charge_windows)

    Examples:
        >>> charge_windows = [{"start": 100, "end": 200, "average": 15.0}]
        >>> charge_limits = [50.0]
        >>> export_windows = [{"start": 150, "end": 250}]
        >>> export_limits = [80.0]
        >>> limits, windows = remove_intersecting_windows(charge_limits, charge_windows, export_limits, export_windows)
        >>> # Result will have charge window clipped to [100, 150] to avoid overlap
    """
    clip_again = True

    # For each charge window
    while clip_again:
        clip_again = False
        new_limit_best = []
        new_window_best = []
        for window_n in range(len(charge_limit_best)):
            window = charge_window_best[window_n]
            start = window["start"]
            end = window["end"]
            average = window["average"]
            limit = charge_limit_best[window_n]
            clipped = False

            # For each discharge window
            for dwindow_n in range(len(export_limit_best)):
                dwindow = export_window_best[dwindow_n]
                dlimit = export_limit_best[dwindow_n]
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
                        if (dstart - start) >= 5:
                            new_window = {}
                            new_window["start"] = start
                            new_window["end"] = dstart
                            new_window["average"] = average
                            new_window_best.append(new_window)
                            new_limit_best.append(limit)
                        start = dend
                        clipped = True
                        if (end - start) >= 5:
                            clip_again = True

            if not clipped or ((end - start) >= 5):
                new_window = {}
                new_window["start"] = start
                new_window["end"] = end
                new_window["average"] = average
                new_window_best.append(new_window)
                new_limit_best.append(limit)

        if clip_again:
            charge_window_best = new_window_best.copy()
            charge_limit_best = new_limit_best.copy()

    return new_limit_best, new_window_best
