# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for battery_rate_grid - the charge and discharge rate grids (SoC x battery temperature)."""

import time
from datetime import date, timedelta

from battery_rate_grid import manual_rate_grid, fit_rate_grid, combine_rate_grid, rate_grid_curve, rate_grid_table, merge_rate_grid_samples
from const import BATTERY_RATE_GRID_MAX

TODAY = "2026-11-15"


def day_offset(days):
    """The date string this many days before TODAY."""
    return (date.fromisoformat(TODAY) - timedelta(days=days)).isoformat()


def check(failed, condition, message):
    """Print a failure and return the updated failed flag."""
    if not condition:
        print("ERROR: {}".format(message))
        return True
    return failed


def taper_samples(temperatures, days=4, knee=70, low=0.3, per_cell=2, discharge=False, extra=None):
    """Synthetic segments: full rate below the knee SoC, `low` above it, at each temperature, spread over `days` days."""
    samples = {}
    for d in range(days):
        day = day_offset(d)
        rows = samples.setdefault(day, [])
        for temp in temperatures:
            for soc in range(10, 100, 4):  # 23 SoC cells per temperature, above BATTERY_RATE_GRID_MIN_CELLS
                rate = 1.0 if soc < knee else low
                stored_soc = 100 - soc if discharge else soc
                for _ in range(per_cell):
                    rows.append([stored_soc + 0.4, temp + 0.3, rate])
    for day, row in extra or []:
        samples.setdefault(day, []).append(row)
    return samples


def monotone(grid, discharge=False):
    """True if every row obeys the SoC rule and every column the temperature rule."""
    rows = grid["rows"]
    for row in rows:
        for p in range(1, 101):
            if discharge and row[p] < row[p - 1] - 1e-9:
                return False
            if not discharge and row[p] > row[p - 1] + 1e-9:
                return False
    for j in range(1, len(rows)):
        for p in range(101):
            if rows[j][p] < rows[j - 1][p] - 1e-9:
                return False
    return True


def test_manual_grid():
    """Manual curves convert to a grid that reproduces today's SoC curve x temperature cap."""
    failed = False
    soc_curve = {100: 0.5, 50: 0.8}
    temperature_curve = {0: 0.0, 5: 0.2, 20: 1.0}
    # No curves at all: today's 1C cap still applies (soc_max 2 kWh, max rate 0.1 kW/min -> 2/60 / 0.1)
    default = manual_rate_grid({}, {}, 2.0, 0.1)
    failed = check(failed, default is not None and default["temp_low"] is None, "no curves: one row expected, got {}".format(default and default["temp_low"]))
    failed = check(failed, abs(rate_grid_curve(default, 15, rated=0.1)[50] - 0.0333333) < 1e-6, "no curves: 1C cap expected, got {}".format(rate_grid_curve(default, 15, rated=0.1)[50]))
    plain = manual_rate_grid({100: 0.5}, {}, 10.0, 0.1)
    failed = check(failed, plain["temp_low"] is None and plain["rows"][0][100] == 0.5 and plain["rows"][0][50] == 1.0, "SoC curve only: row should carry the curve, got {} / {}".format(plain["rows"][0][100], plain["rows"][0][50]))
    grid = manual_rate_grid(soc_curve, temperature_curve, 10.0, 0.1)
    failed = check(failed, grid["temp_low"] is not None, "temperature curve: rows by temperature expected")
    # Pinned from utils.get_charge_rate_curve before the grid replaced it (soc 5.0 of 10 kWh = 50%, max rate 0.1)
    for temperature, expected in ((5, 0.03333333333333333), (5.9, 0.03333333333333333), (35, 0.08000000000000002), (-30, 0.0), (0, 0.0), (12, 0.08000000000000002)):
        value = rate_grid_curve(grid, temperature, rated=0.1)[50]
        failed = check(failed, abs(value - expected) < 1e-9, "manual grid at {} C: expected {} got {}".format(temperature, expected, value))
    failed = check(failed, grid["zero_row"][50] == 0.0, "exact 0 uses the curve's 0 entry, got {}".format(grid["zero_row"][50]))
    return failed


def test_fit_rule_and_noise():
    """The fit obeys the rule, follows the taper and does not follow a single noisy cell."""
    failed = False
    samples = taper_samples([10, 20], extra=[(day_offset(0), [80.4, 20.3, 0.9])])
    start = time.time()
    learned = fit_rate_grid(samples, TODAY, False, None)
    print("fit_rate_grid: {:.3f} s".format(time.time() - start))
    failed = check(failed, learned is not None, "fit: enough data should learn a grid")
    if learned:
        failed = check(failed, monotone(learned), "fit: the rule is broken")
        row_20 = learned["rows"][20 - learned["temp_low"]]
        failed = check(failed, row_20[90] < 0.5, "fit: 90% SoC at 20 C should be tapered, got {}".format(row_20[90]))
        failed = check(failed, row_20[30] > 0.95, "fit: 30% SoC at 20 C should be full rate, got {}".format(row_20[30]))
        failed = check(failed, learned["temp_range"] == [10, 20], "fit: temperature range expected [10, 20], got {}".format(learned["temp_range"]))
    discharge = fit_rate_grid(taper_samples([10, 20], discharge=True), TODAY, True, None)
    failed = check(failed, discharge is not None and monotone(discharge, discharge=True), "discharge fit: rule reversed along SoC")
    if discharge:
        row = discharge["rows"][-1]
        failed = check(failed, row[10] < 0.5 and row[70] > 0.95, "discharge fit: low SoC should taper, got {} / {}".format(row[10], row[70]))
    return failed


def test_fit_thresholds():
    """Too few cells or days learns nothing."""
    failed = False
    few_cells = {day_offset(d): [[soc + 0.5, 20.0, 1.0] for soc in range(10, 15)] for d in range(4)}
    failed = check(failed, fit_rate_grid(few_cells, TODAY, False, None) is None, "fit: 5 cells should not learn")
    few_days = taper_samples([10, 20], days=2)
    failed = check(failed, fit_rate_grid(few_days, TODAY, False, None) is None, "fit: 2 days should not learn")
    return failed


def test_fit_no_temperature():
    """Segments with no temperature learn a single row."""
    failed = False
    samples = {day: [[row[0], None, row[2]] for row in rows] for day, rows in taper_samples([20]).items()}
    learned = fit_rate_grid(samples, TODAY, False, None)
    failed = check(failed, learned is not None and learned["temp_low"] is None and len(learned["rows"]) == 1, "no temperature: one row expected")
    return failed


def test_fit_weights_and_cap():
    """Old evidence counts for less, and values are capped."""
    failed = False
    samples = taper_samples([20])
    samples.setdefault(day_offset(300), []).extend([[50.5, 20.0, 0.2]] * 3)
    samples[day_offset(0)].extend([[50.5, 20.0, 0.9]] * 3)
    learned = fit_rate_grid(samples, TODAY, False, None)
    value = learned["rows"][0][50] if learned else 0
    failed = check(failed, value > 0.8, "weights: fresh evidence should dominate, got {}".format(value))
    hot = taper_samples([20], extra=[(day_offset(0), [20.5, 20.0, 1.5])] * 20)
    learned = fit_rate_grid(hot, TODAY, False, None)
    value = max(max(row) for row in learned["rows"]) if learned else 0
    failed = check(failed, value <= BATTERY_RATE_GRID_MAX + 1e-9 and value > 1.1, "cap: expected at most {}, got {}".format(BATTERY_RATE_GRID_MAX, value))
    return failed


def test_combine_and_lookup():
    """The engine grid clamps outside the evidence, applies a manual cold curve below it, and treats exact 0 as unknown."""
    failed = False
    learned = fit_rate_grid(taper_samples([10, 15, 20], low=0.3), TODAY, False, None)
    engine = combine_rate_grid(learned, None)
    row_10 = tuple(learned["rows"][0])
    failed = check(failed, rate_grid_curve(engine, 0.5) == row_10, "below the evidence: clamp to the coldest learned row")
    failed = check(failed, rate_grid_curve(engine, -15) == row_10, "far below the evidence: still the coldest learned row")
    failed = check(failed, rate_grid_curve(engine, 0) == tuple(learned["rows"][-1]), "exact 0: the 20 C row")
    manual = manual_rate_grid({}, {5: 0.06, 20: 1.0}, 10.0, 1.0 / 6)  # 0.06C on 10 kWh = 0.01 kW/min = 0.06 of a 10 kW (1/6 kW/min) rating
    engine = combine_rate_grid(learned, manual)
    failed = check(failed, abs(rate_grid_curve(engine, 5)[50] - 0.06) < 1e-9, "manual cold curve applies below the evidence, got {}".format(rate_grid_curve(engine, 5)[50]))
    failed = check(failed, rate_grid_curve(engine, 15) == tuple(learned["rows"][15 - learned["temp_low"]]), "inside the evidence the learned row is used")
    absolute = rate_grid_curve(engine, 15, rated=0.1)
    failed = check(failed, abs(absolute[30] - 0.1 * learned["rows"][5][30]) < 1e-12, "rated scales the row to kW/minute")
    failed = check(failed, combine_rate_grid(None, manual) is manual, "no learned grid: the manual grid is used")
    failed = check(failed, rate_grid_curve(None, 12, rated=0.2) == (0.2,) * 101, "no grid at all: full rated rate")
    return failed


def test_table_and_merge():
    """The coarse table and the per-day merge."""
    failed = False
    learned = fit_rate_grid(taper_samples([10, 20]), TODAY, False, None)
    table = rate_grid_table(combine_rate_grid(learned, None))
    failed = check(failed, table["soc"] == list(range(0, 101, 10)), "table: SoC columns every 10%, got {}".format(table["soc"]))
    failed = check(failed, table["temperature"] is not None and all(t % 5 == 0 for t in table["temperature"]), "table: temperature rows every 5 C")
    stored = {day_offset(400): [[50, 20, 1.0]], day_offset(10): [[50, 20, 1.0]], day_offset(2): [[50, 20, 1.0], [60, 20, 1.0]]}
    scanned = {day_offset(2): [[50, 20, 0.5]], day_offset(1): [[40, 20, 0.7]]}
    merged = merge_rate_grid_samples(stored, scanned, TODAY, 365, covered_from_day=day_offset(2))
    failed = check(failed, day_offset(400) not in merged, "merge: days over the age limit are dropped")
    failed = check(failed, merged.get(day_offset(10)) == [[50, 20, 1.0]], "merge: days before the scan are kept")
    failed = check(failed, merged.get(day_offset(2)) == stored[day_offset(2)], "merge: the partly covered day keeps the larger stored list")
    failed = check(failed, merged.get(day_offset(1)) == [[40, 20, 0.7]], "merge: a fully covered day is replaced by the scan")
    return failed


def run_battery_rate_grid_tests(my_predbat=None):
    """Run the battery rate grid tests."""
    failed = False
    for test in (test_manual_grid, test_fit_rule_and_noise, test_fit_thresholds, test_fit_no_temperature, test_fit_weights_and_cap, test_combine_and_lookup, test_table_and_merge):
        print("**** {} ****".format(test.__name__))
        failed = test() or failed
    return failed
