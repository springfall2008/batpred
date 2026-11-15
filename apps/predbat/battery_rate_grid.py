# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Battery rate grids: the charge (or discharge) rate a battery achieves, as a fraction of its rated rate, by SoC % and
battery temperature.

A grid has 101 SoC columns (0..100%) and one row per whole degree, or a single row for every temperature
("temp_low": None) when there is no temperature evidence or curve. It is either built from the manual curves
(manual_rate_grid) or learned from full-rate history segments (fit_rate_grid) under a monotone rule:

- charge: the rate may only stay the same or drop as SoC rises, or as the temperature falls
- discharge: the rate may only stay the same or drop as SoC falls, or as the temperature falls

combine_rate_grid() lays a learned grid over the manual one into the engine grid the plan reads with rate_grid_curve(),
one 101-entry SoC curve per temperature. Grid values are fractions of the rated rate, so the ceiling on every path
(AC charge, DC charge, export) is the same kW: rated x value, under that path's own maximum.
"""

from datetime import date

from const import (
    BATTERY_RATE_GRID_EMPTY_WEIGHT,
    BATTERY_RATE_GRID_HALF_LIFE_DAYS,
    BATTERY_RATE_GRID_MAX,
    BATTERY_RATE_GRID_MAX_ITER,
    BATTERY_RATE_GRID_MIN_CELLS,
    BATTERY_RATE_GRID_MIN_DAYS,
    BATTERY_RATE_GRID_TEMP_MAX,
    BATTERY_RATE_GRID_TEMP_MIN,
    BATTERY_RATE_GRID_TOLERANCE,
    BATTERY_RATE_GRID_UNKNOWN_TEMP,
)
from utils import battery_temperature_index, find_battery_temperature_cap, get_curve_value

COLUMNS = 101


def grid_temperature_index(temperature):
    """The grid row degree for a temperature reading (whole degree, -20..BATTERY_RATE_GRID_TEMP_MAX, exact 0 kept as 0)."""
    return battery_temperature_index(temperature, temp_max=BATTERY_RATE_GRID_TEMP_MAX)


def grid_row(grid, degree):
    """The row of a grid for a whole degree, clamped to the grid's range."""
    rows = grid["rows"]
    if grid["temp_low"] is None:
        return rows[0]
    return rows[min(max(degree - grid["temp_low"], 0), len(rows) - 1)]


def grid_cap(grid, degree):
    """The absolute temperature cap a grid carries for a whole degree (fraction of rated), or None when it has none."""
    caps = grid.get("caps") if grid else None
    if not caps:
        return None
    if grid["temp_low"] is None:
        return caps[0]
    return caps[min(max(degree - grid["temp_low"], 0), len(caps) - 1)]


def manual_rate_grid(soc_curve, temperature_curve, soc_max, max_rate):
    """
    The grid that reproduces the manual curves: min(SoC curve, temperature cap / max_rate) for every cell.

    The temperature caps are also kept on their own ("caps" per row, "zero_cap" for an exact 0 reading), as fractions of
    max_rate: they cap a learned grid where it has no evidence, and give the absolute cap on a path above the rated rate.

    The temperature cap is utils.find_battery_temperature_cap, so its rules carry over unchanged (including its 1C cap
    when no temperature curve is set). With no temperature curve the grid has a single row.

    Args:
        soc_curve: battery_charge_power_curve (or discharge) as {soc_percent: fraction}, may be empty
        temperature_curve: battery_temperature_charge_curve (or discharge) as {degree: C-rate}, may be empty
        soc_max: battery size in kWh
        max_rate: the rated charge (or discharge) rate in kW per minute
    """
    column = [get_curve_value(soc_curve or {}, percent, 1.0) for percent in range(COLUMNS)]

    def cap_at(temperature):
        """The temperature cap at one temperature, as a fraction of max_rate."""
        cap = find_battery_temperature_cap(temperature, temperature_curve or {}, soc_max, float("inf"))
        return cap / max_rate if max_rate > 0 else 1.0

    def row_at(cap):
        """Row under one temperature cap."""
        return [min(value, cap) for value in column]

    if not temperature_curve:
        cap = cap_at(BATTERY_RATE_GRID_UNKNOWN_TEMP)
        row = row_at(cap)
        return {"temp_low": None, "rows": [row], "zero_row": row, "caps": [cap], "zero_cap": cap}
    caps = [cap_at(degree) for degree in range(BATTERY_RATE_GRID_TEMP_MIN, BATTERY_RATE_GRID_TEMP_MAX + 1)]
    zero_cap = cap_at(0)
    return {"temp_low": BATTERY_RATE_GRID_TEMP_MIN, "rows": [row_at(cap) for cap in caps], "zero_row": row_at(zero_cap), "caps": caps, "zero_cap": zero_cap}


def pava_non_increasing(values, weights):
    """Weighted least-squares non-increasing fit of a sequence (pool adjacent violators)."""
    blocks = []  # [weighted sum, weight, length]
    for value, weight in zip(reversed(values), reversed(weights)):
        blocks.append([value * weight, weight, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            total, weight_sum, length = blocks.pop()
            blocks[-1][0] += total
            blocks[-1][1] += weight_sum
            blocks[-1][2] += length
    result = []
    for total, weight_sum, length in blocks:
        result.extend([total / weight_sum] * length)
    result.reverse()
    return result


def pava_non_decreasing(values, weights):
    """Weighted least-squares non-decreasing fit of a sequence."""
    return list(reversed(pava_non_increasing(list(reversed(values)), list(reversed(weights)))))


def four_nearest(measured, column, row, n_rows):
    """
    Estimate an empty cell from the nearest measured cell each way along its row and its column.

    Along each axis: linear between the two when both exist, a copy of the one when only one does (never extrapolated);
    the two axes are blended by inverse distance. With nothing on either axis, the nearest measured cell.
    """
    estimates = []
    for d_column, d_row, size, position in ((1, 0, COLUMNS, column), (0, 1, n_rows, row)):
        found = []
        for sign in (-1, 1):
            step = 1
            while 0 <= position + sign * step < size:
                key = (column + sign * d_column * step, row + sign * d_row * step)
                if key in measured:
                    found.append((measured[key], step))
                    break
                step += 1
        if len(found) == 2:
            (value_a, distance_a), (value_b, distance_b) = found
            estimates.append(((value_a * distance_b + value_b * distance_a) / (distance_a + distance_b), min(distance_a, distance_b)))
        elif found:
            estimates.append(found[0])
    if estimates:
        weight = sum(1.0 / distance for _, distance in estimates)
        return sum(value / distance for value, distance in estimates) / weight
    nearest = min(measured, key=lambda key: abs(key[0] - column) + abs(key[1] - row))
    return measured[nearest]


def isotonic_grid(targets, weights):
    """
    Weighted least-squares grid [row][column] that is non-increasing along each row and non-decreasing up each column.

    Dykstra's alternating projections between the two sets of chain constraints (each a weighted PAVA), stopped at
    BATTERY_RATE_GRID_TOLERANCE or BATTERY_RATE_GRID_MAX_ITER, then a dominance envelope (each cell at most every cell
    to its left or above) to remove what is left of the iteration error.
    """
    n_rows = len(targets)
    current = [row[:] for row in targets]
    residual_rows = [[0.0] * COLUMNS for _ in range(n_rows)]
    residual_columns = [[0.0] * COLUMNS for _ in range(n_rows)]
    for _ in range(BATTERY_RATE_GRID_MAX_ITER):
        shifted = [[current[r][c] + residual_rows[r][c] for c in range(COLUMNS)] for r in range(n_rows)]
        by_row = [pava_non_increasing(shifted[r], weights[r]) for r in range(n_rows)]
        residual_rows = [[shifted[r][c] - by_row[r][c] for c in range(COLUMNS)] for r in range(n_rows)]
        shifted = [[by_row[r][c] + residual_columns[r][c] for c in range(COLUMNS)] for r in range(n_rows)]
        result = [[0.0] * COLUMNS for _ in range(n_rows)]
        for c in range(COLUMNS):
            fitted = pava_non_decreasing([shifted[r][c] for r in range(n_rows)], [weights[r][c] for r in range(n_rows)])
            for r in range(n_rows):
                result[r][c] = fitted[r]
        residual_columns = [[shifted[r][c] - result[r][c] for c in range(COLUMNS)] for r in range(n_rows)]
        change = max(abs(result[r][c] - current[r][c]) for r in range(n_rows) for c in range(COLUMNS))
        current = result
        if change < BATTERY_RATE_GRID_TOLERANCE:
            break
    # Envelope: lower SoC (left) and warmer (row above) cells are at least as fast
    for r in range(n_rows - 1, -1, -1):
        for c in range(COLUMNS):
            best = current[r][c]
            if c > 0:
                best = min(best, current[r][c - 1])
            if r < n_rows - 1:
                best = min(best, current[r + 1][c])
            current[r][c] = best
    return current


def fit_rate_grid(samples, today, discharge, prior):
    """
    Learn a grid from history segments, or None when there is not enough evidence.

    Args:
        samples: {day "YYYY-MM-DD": [[soc_percent, temperature or None, rate fraction], ...]}
        today: today's day string, for the age weighting
        discharge: True for the discharge grid (the SoC rule is reversed)
        prior: the manual grid (empty cells lean towards it), or None (empty cells lean towards their measured neighbours)

    Returns:
        {"temp_low", "rows", "zero_row", "cells", "days", "segments", "temp_range": [low, high] or None} over the
        measured temperature range only, or None below BATTERY_RATE_GRID_MIN_CELLS cells or BATTERY_RATE_GRID_MIN_DAYS days.
    """
    today_date = date.fromisoformat(today)
    has_temperature = any(entry[1] is not None for entries in samples.values() for entry in entries)
    cells = {}  # (column, degree) -> [weighted rate sum, weight, segments]
    days = set()
    segments = 0
    for day, entries in samples.items():
        try:
            age = max((today_date - date.fromisoformat(day)).days, 0)
        except (TypeError, ValueError):
            continue
        weight = 0.5 ** (age / BATTERY_RATE_GRID_HALF_LIFE_DAYS)
        for soc, temperature, rate in entries:
            if has_temperature and temperature is None:
                continue
            column = min(max(int(soc), 0), COLUMNS - 1)
            if discharge:
                column = COLUMNS - 1 - column
            degree = grid_temperature_index(temperature) if has_temperature else 0
            cell = cells.setdefault((column, degree), [0.0, 0.0, 0])
            cell[0] += rate * weight
            cell[1] += weight
            cell[2] += 1
            days.add(day)
            segments += 1
    if len(cells) < BATTERY_RATE_GRID_MIN_CELLS or len(days) < BATTERY_RATE_GRID_MIN_DAYS:
        return None

    degrees = [degree for _, degree in cells]
    low, high = min(degrees), max(degrees)
    n_rows = high - low + 1
    measured = {(column, degree - low): total / weight for (column, degree), (total, weight, _) in cells.items()}
    targets = [[0.0] * COLUMNS for _ in range(n_rows)]
    weights = [[BATTERY_RATE_GRID_EMPTY_WEIGHT] * COLUMNS for _ in range(n_rows)]
    for r in range(n_rows):
        for c in range(COLUMNS):
            if (c, r) in measured:
                targets[r][c] = measured[(c, r)]
                weights[r][c] = cells[(c, r + low)][1]
            elif prior is not None:
                targets[r][c] = grid_row(prior, r + low)[COLUMNS - 1 - c if discharge else c]
            else:
                targets[r][c] = four_nearest(measured, c, r, n_rows)
    fitted = isotonic_grid(targets, weights)
    rows = []
    for row in fitted:
        values = [round(min(max(value, 0.0), BATTERY_RATE_GRID_MAX), 4) for value in row]
        rows.append(values[::-1] if discharge else values)
    temp_low = low if has_temperature else None
    learned = {"temp_low": temp_low, "rows": rows, "cells": len(cells), "days": len(days), "segments": segments, "temp_range": [low, high] if has_temperature else None}
    learned["zero_row"] = grid_row(learned, BATTERY_RATE_GRID_UNKNOWN_TEMP)
    return learned


def combine_rate_grid(learned, manual):
    """
    The engine grid: the learned grid where there is evidence, the manual temperature cap beyond it.

    Inside the learned temperature range the learned rows are used as they are. Colder, the coldest learned row is used
    (never extrapolated), capped by the manual temperature cap at that degree, so a manual cold curve still applies below
    the evidence (the manual SoC curve does not: the learned rows already carry the SoC taper). Warmer, the warmest
    learned row is used as it is: capping it would make a warmer row slower than the warmest learned one. A learned grid
    with no temperature (one row) is capped by the manual temperature cap at every degree. An exact 0 reading (missing)
    uses the learned BATTERY_RATE_GRID_UNKNOWN_TEMP row under the manual exact-0 cap. With nothing learned, the manual
    grid as it is. The caps applied are kept ("caps", "zero_cap") for paths above the rated rate (rate_grid_curve).
    """
    if learned is None:
        return manual
    has_manual_temperature = manual is not None and manual.get("temp_low") is not None
    zero_cap = manual.get("zero_cap") if manual else None

    def capped(row, cap):
        """A row under a cap (None: no cap)."""
        return row if cap is None else [min(value, cap) for value in row]

    if learned["temp_low"] is None:
        row = learned["rows"][0]
        if not has_manual_temperature:
            cap = grid_cap(manual, BATTERY_RATE_GRID_UNKNOWN_TEMP)
            return {"temp_low": None, "rows": [capped(row, cap)], "zero_row": capped(row, cap), "caps": [cap], "zero_cap": cap}
        caps = [grid_cap(manual, degree) for degree in range(BATTERY_RATE_GRID_TEMP_MIN, BATTERY_RATE_GRID_TEMP_MAX + 1)]
        return {"temp_low": BATTERY_RATE_GRID_TEMP_MIN, "rows": [capped(row, cap) for cap in caps], "zero_row": capped(row, zero_cap), "caps": caps, "zero_cap": zero_cap}
    low = learned["temp_low"]
    rows, caps = [], []
    for degree in range(BATTERY_RATE_GRID_TEMP_MIN, BATTERY_RATE_GRID_TEMP_MAX + 1):
        cap = grid_cap(manual, degree) if degree < low else None
        rows.append(capped(grid_row(learned, degree), cap))
        caps.append(cap)
    return {"temp_low": BATTERY_RATE_GRID_TEMP_MIN, "rows": rows, "zero_row": capped(grid_row(learned, BATTERY_RATE_GRID_UNKNOWN_TEMP), zero_cap), "caps": caps, "zero_cap": zero_cap}


def rate_grid_curve(grid, temperature, rated=1.0):
    """
    The SoC curve for one temperature: 101 values (rated x the grid value), then rated itself (entry 101), then the
    absolute temperature cap rated x the grid's cap at that temperature (entry 102, infinite when it has none).

    With rated as the battery's rated rate (kW per minute) the values are the battery's ceiling in kW per minute, and
    entries 101 and 102 let utils.rate_curve_ceiling() scale it for a path whose maximum is above the rated rate while
    a manual temperature cap stays absolute. With no grid it is the rated rate everywhere. An exact 0 reading uses the
    grid's zero_row.
    """
    if grid is None:
        return (rated,) * COLUMNS + (rated, float("inf"))
    if grid["temp_low"] is None:
        row = grid["rows"][0]
        cap = grid_cap(grid, 0)
    elif temperature == 0:
        row = grid["zero_row"]
        cap = grid.get("zero_cap")
    else:
        degree = grid_temperature_index(temperature)
        row = grid_row(grid, degree)
        cap = grid_cap(grid, degree)
    return tuple(value * rated for value in row) + (rated, float("inf") if cap is None else cap * rated)


def rate_grid_table(grid, soc_step=10, temp_step=5):
    """A coarse copy of a grid for sensors and the web page: {"soc": [...], "temperature": [...] or None, "rows": [[...]]}."""
    socs = list(range(0, COLUMNS, soc_step))
    if grid is None:
        return {"soc": socs, "temperature": None, "rows": [[1.0] * len(socs)]}
    if grid["temp_low"] is None:
        return {"soc": socs, "temperature": None, "rows": [[round(grid["rows"][0][p], 2) for p in socs]]}
    low = grid["temp_low"]
    temperatures = [degree for degree in range(low, low + len(grid["rows"])) if degree % temp_step == 0]
    return {"soc": socs, "temperature": temperatures, "rows": [[round(grid_row(grid, degree)[p], 2) for p in socs] for degree in temperatures]}


def merge_rate_grid_samples(stored, scanned, today, max_age_days, covered_from_day=None):
    """
    Merge a history scan's {day: [segments]} into the stored samples, dropping days over max_age_days old.

    A day the scan covered completely (after covered_from_day) is replaced by what the scan found, or removed when it
    found nothing. The partly covered day, and every day when covered_from_day is not known, keeps whichever of the
    stored and scanned lists is longer. Days before the scan are kept as stored.
    """
    today_date = date.fromisoformat(today)

    def fresh(day):
        """True if the day is within max_age_days."""
        try:
            return (today_date - date.fromisoformat(day)).days <= max_age_days
        except (TypeError, ValueError):
            return False

    merged = {}
    for day in set(stored or {}) | set(scanned or {}):
        if not fresh(day):
            continue
        old = (stored or {}).get(day)
        new = (scanned or {}).get(day)
        if covered_from_day is not None and day > covered_from_day:
            value = new
        elif covered_from_day is not None and day < covered_from_day:
            value = old if old is not None else new
        else:
            value = new if new is not None and len(new) >= len(old or []) else old
        if value:
            merged[day] = value
    return merged


def clean_rate_grid_samples(data):
    """Samples read back from storage, with anything malformed dropped: {day: [[soc %, temperature or None, rate], ...]}."""
    cleaned = {}
    if not isinstance(data, dict):
        return cleaned
    for day, entries in data.items():
        try:
            date.fromisoformat(day)
        except (TypeError, ValueError):
            continue
        if not isinstance(entries, list):
            continue
        rows = []
        for entry in entries:
            if not isinstance(entry, (list, tuple)) or len(entry) != 3:
                continue
            soc, temperature, rate = entry
            if not isinstance(soc, (int, float)) or not isinstance(rate, (int, float)) or isinstance(soc, bool) or isinstance(rate, bool):
                continue
            if temperature is not None and (not isinstance(temperature, (int, float)) or isinstance(temperature, bool)):
                continue
            rows.append([soc, temperature, rate])
        if rows:
            cleaned[day] = rows
    return cleaned
