# -----------------------------------------------------------------------------
# Predbat Home Battery System - Data Formatting Utilities
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""
Pure data formatting functions extracted from utils.py

This module contains stateless, side-effect-free functions for data formatting.
All functions are unit tested and maintain backward compatibility.
"""

from typing import Union


def dp0(value: Union[int, float]) -> int:
    """
    Round to 0 decimal places.

    Args:
        value: Numeric value to round

    Returns:
        Rounded integer value

    Examples:
        >>> dp0(5.7)
        6
        >>> dp0(5.3)
        5
        >>> dp0(5.5)
        6
    """
    return round(value)


def dp1(value: Union[int, float]) -> float:
    """
    Round to 1 decimal place.

    Args:
        value: Numeric value to round

    Returns:
        Value rounded to 1 decimal place

    Examples:
        >>> dp1(5.67)
        5.7
        >>> dp1(5.63)
        5.6
        >>> dp1(5.0)
        5.0
    """
    return round(value, 1)


def dp2(value: Union[int, float]) -> float:
    """
    Round to 2 decimal places.

    Args:
        value: Numeric value to round

    Returns:
        Value rounded to 2 decimal places

    Examples:
        >>> dp2(5.678)
        5.68
        >>> dp2(5.673)
        5.67
        >>> dp2(5.0)
        5.0
    """
    return round(value, 2)


def dp3(value: Union[int, float]) -> float:
    """
    Round to 3 decimal places.

    Args:
        value: Numeric value to round

    Returns:
        Value rounded to 3 decimal places

    Examples:
        >>> dp3(5.6789)
        5.679
        >>> dp3(5.6784)
        5.678
        >>> dp3(5.0)
        5.0
    """
    return round(value, 3)


def dp4(value: Union[int, float]) -> float:
    """
    Round to 4 decimal places.

    Args:
        value: Numeric value to round

    Returns:
        Value rounded to 4 decimal places

    Examples:
        >>> dp4(5.67891)
        5.6789
        >>> dp4(5.67895)
        5.679
        >>> dp4(5.0)
        5.0
    """
    return round(value, 4)
