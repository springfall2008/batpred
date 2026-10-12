# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the export energy budget (export_budget_mode)"""

import copy
from datetime import timedelta

from const import PV_SCENARIO_NOMINAL, TIME_FORMAT
from prediction import Prediction
from utils import unpack_export_limit
from tests.test_infra import FIXTURE_MINUTES_NOW, reset_inverter, reset_rates
from export_budget import (
    EXPORT_BUDGET_OFF,
    EXPORT_BUDGET_PV_ORIGIN,
    EXPORT_BUDGET_PV_EQUIVALENT,
    parse_export_budget_mode,
    export_budget_battery_export_allowed,
    export_budget_house_from_battery,
    export_budget_update,
    export_budget_replay,
)


def reset_budget_base(my_predbat, minutes_now, import_rate=10.0, export_rate=5.0):
    """Put my_predbat into a known state for the export budget scenarios.

    Earlier tests leave overrides on the shared fixture, so everything these scenarios depend on is set
    explicitly here rather than inherited.
    """
    reset_inverter(my_predbat)
    # After reset_inverter, which pins minutes_now to the fixture clock, and before reset_rates so the
    # rate tables cover the shifted horizon
    my_predbat.minutes_now = minutes_now
    reset_rates(my_predbat, import_rate, export_rate)
    my_predbat.battery_rate_max_export = my_predbat.battery_rate_max_discharge
    my_predbat.inverter_hybrid = False
    my_predbat.inverter_can_charge_during_export = True
    my_predbat.inverter_support_feedin_first = False
    my_predbat.inverter_freeze_export_discharge_rate = 0.0
    my_predbat.set_charge_freeze = False
    my_predbat.set_reserve_enable = False
    my_predbat.set_export_freeze = False
    my_predbat.set_export_freeze_only = False
    my_predbat.set_discharge_during_charge = True
    my_predbat.set_export_low_power = False
    my_predbat.battery_charge_power_curve = {}
    my_predbat.battery_discharge_power_curve = {}
    my_predbat.battery_temperature = 20
    my_predbat.battery_temperature_prediction = {}
    my_predbat.best_soc_keep = 0.0
    my_predbat.best_soc_keep_weight = 0.5
    my_predbat.io_adjusted = {}
    my_predbat.all_active_keep = {}
    my_predbat.all_active_keep_max = {}
    my_predbat.carbon_enable = False
    my_predbat.carbon_intensity = {}
    my_predbat.num_cars = 0
    my_predbat.car_energy_reported_load = True
    my_predbat.iboost_enable = False
    my_predbat.iboost_plan = []
    my_predbat.rate_gas = {}
    my_predbat.export_budget_mode = 0
    my_predbat.export_budget_cap_kwh = 0.0
    my_predbat.export_budget_seed = None


def close(a, b, tol=1e-9):
    """Float comparison helper"""
    return abs(a - b) <= tol


def cumulative(steps, step=5):
    """Build a Predbat-style cumulative series keyed by minutes ago from per-step energies listed oldest first"""
    total_minutes = len(steps) * step
    data = {}
    running = 0.0
    data[total_minutes] = 0.0
    for index, energy in enumerate(steps):
        running += energy
        data[total_minutes - (index + 1) * step] = running
    return data


def check(failed, condition, message):
    """Print and record a failure"""
    if not condition:
        print("ERROR: export_budget: " + message)
        return True
    return failed


def test_parse_mode():
    """apps.yaml values map to mode constants, unknown values to None"""
    failed = False
    failed = check(failed, parse_export_budget_mode(None) == EXPORT_BUDGET_OFF, "None -> off")
    failed = check(failed, parse_export_budget_mode(False) == EXPORT_BUDGET_OFF, "False -> off")
    failed = check(failed, parse_export_budget_mode("off") == EXPORT_BUDGET_OFF, "off")
    failed = check(failed, parse_export_budget_mode(" PV_Origin ") == EXPORT_BUDGET_PV_ORIGIN, "pv_origin, case/space insensitive")
    failed = check(failed, parse_export_budget_mode("pv_equivalent") == EXPORT_BUDGET_PV_EQUIVALENT, "pv_equivalent")
    failed = check(failed, parse_export_budget_mode("pv_origin_house_first") is None, "removed pv_origin_house_first -> None")
    failed = check(failed, parse_export_budget_mode("surplus_only") is None, "unknown -> None")
    failed = check(failed, parse_export_budget_mode(3) is None, "number -> None")
    return failed


def test_allowed():
    """Allowed battery export per mode"""
    failed = False
    # pv_equivalent: bucket 1 + pv 0.3 - PV already exported directly 0.2
    failed = check(failed, close(export_budget_battery_export_allowed(EXPORT_BUDGET_PV_EQUIVALENT, 1.0, 0, 0, 0.3, 0.2, 0, 1.0), 1.1), "pv_equivalent allowance")
    failed = check(failed, close(export_budget_battery_export_allowed(EXPORT_BUDGET_PV_EQUIVALENT, 0.0, 0, 0, 0.1, 0.4, 0, 1.0), 0.0), "pv_equivalent floor 0")
    # pv_origin: house takes 0.5 stored, grid part 0.3 covers part of it, the rest (0.2) comes from pv 1.0 -> 0.8 left
    failed = check(failed, close(export_budget_battery_export_allowed(EXPORT_BUDGET_PV_ORIGIN, 0, 1.0, 0.3, 0, 0, 0.5, 1.0), 0.8), "pv_origin house uses grid first")
    # stored_to_ac 0.9: house AC 0.45 = 0.5 stored, all from grid part 2.0, pv 1.0 left -> 0.9 AC
    failed = check(failed, close(export_budget_battery_export_allowed(EXPORT_BUDGET_PV_ORIGIN, 0, 1.0, 2.0, 0, 0, 0.45, 0.9), 0.9), "pv_origin losses")
    return failed


def test_house_from_battery():
    """Battery output that counts as serving on-site load: pv_origin all of it up to the load, pv_equivalent only what PV leaves uncovered"""
    failed = False
    # PV 1.0 covers the 0.5 load: pv_origin counts 0.5 of the battery's 0.8 as house, pv_equivalent none
    failed = check(failed, close(export_budget_house_from_battery(EXPORT_BUDGET_PV_ORIGIN, 0.8, 0.5, 1.0), 0.5), "pv_origin PV covers load")
    failed = check(failed, close(export_budget_house_from_battery(EXPORT_BUDGET_PV_EQUIVALENT, 0.8, 0.5, 1.0), 0.0), "pv_equivalent PV covers load")
    # PV 0.2 of a 0.5 load: pv_origin 0.5, pv_equivalent 0.3
    failed = check(failed, close(export_budget_house_from_battery(EXPORT_BUDGET_PV_ORIGIN, 0.8, 0.5, 0.2), 0.5), "pv_origin part load")
    failed = check(failed, close(export_budget_house_from_battery(EXPORT_BUDGET_PV_EQUIVALENT, 0.8, 0.5, 0.2), 0.3), "pv_equivalent part load")
    # Never more than the battery delivers
    failed = check(failed, close(export_budget_house_from_battery(EXPORT_BUDGET_PV_ORIGIN, 0.3, 0.5, 0.0), 0.3), "pv_origin capped by battery")
    return failed


def test_update_pv_equivalent():
    """Bucket fills with PV, drains with export, clamps to [0, cap]"""
    failed = False
    bucket, _, _ = export_budget_update(EXPORT_BUDGET_PV_EQUIVALENT, 1.0, 0, 0, 5, 5, 0, 0.5, 1.0, 0.2, 7.1)
    failed = check(failed, close(bucket, 1.3), "fill/drain")
    bucket, _, _ = export_budget_update(EXPORT_BUDGET_PV_EQUIVALENT, 7.0, 0, 0, 5, 5, 0, 0.5, 1.0, 0.0, 7.1)
    failed = check(failed, close(bucket, 7.1), "cap")
    bucket, _, _ = export_budget_update(EXPORT_BUDGET_PV_EQUIVALENT, 0.1, 0, 0, 5, 5, 0, 0.0, 1.0, 0.5, 7.1)
    failed = check(failed, close(bucket, 0.0), "floor")
    return failed


def test_update_pv_origin():
    """Charge is PV up to the step's PV; discharge serves the house from grid first, export from PV"""
    failed = False
    # Charge 0.5 DC with 0.2 PV: soc 1 -> 1.5 lossless, 40% PV
    _, pv, grid = export_budget_update(EXPORT_BUDGET_PV_ORIGIN, 0, 0.5, 0.5, 1.0, 1.5, 0.5, 0.2, 1.0, 0, 10)
    failed = check(failed, close(pv, 0.7) and close(grid, 0.8), "charge split {} {}".format(pv, grid))
    # Discharge 1.0, 40% to the house: house 0.4 from grid (0.8 -> 0.4), export 0.6 from pv (0.7 -> 0.1)
    _, pv, grid = export_budget_update(EXPORT_BUDGET_PV_ORIGIN, 0, 0.7, 0.8, 1.5, 0.5, 0, 0, 0.4, 0, 10)
    failed = check(failed, close(pv, 0.1) and close(grid, 0.4), "discharge split {} {}".format(pv, grid))
    # Parts renormalise to the SoC after a clamp
    _, pv, grid = export_budget_update(EXPORT_BUDGET_PV_ORIGIN, 0, 1.0, 1.0, 2.0, 1.0, 0, 0, 1.0, 0, 10)
    failed = check(failed, close(pv + grid, 1.0) and pv >= 0 and grid >= 0, "renormalise {} {}".format(pv, grid))
    # Empty parts with SoC left (e.g. reserve) count as grid
    _, pv, grid = export_budget_update(EXPORT_BUDGET_PV_ORIGIN, 0, 0.0, 0.0, 0.5, 0.5, 0, 0, 1.0, 0, 10)
    failed = check(failed, close(pv, 0.0) and close(grid, 0.5), "empty parts -> grid")
    return failed


def test_replay():
    """Seed replay from measured cumulative history"""
    failed = False
    # pv_equivalent: 12 steps of 1 kWh PV and 0.2 export -> 9.6, capped at 7.1
    n = 12
    seed, noncompliant, steps = export_budget_replay(EXPORT_BUDGET_PV_EQUIVALENT, cumulative([1.0] * n), cumulative([0.2] * n), cumulative([0.0] * n), cumulative([0.0] * n), 10.0, 7.1)
    failed = check(failed, steps == n and close(seed[0], 7.1) and close(noncompliant, 0.0), "pv_equivalent sunny {} {} {}".format(seed, noncompliant, steps))
    # pv_origin: 6 steps grid charge 1 kWh, then 6 steps PV charge 1 kWh -> share 0.5
    pv = [0.0] * 6 + [1.0] * 6
    imp = [1.0] * 6 + [0.0] * 6
    zero = [0.0] * 12
    seed, noncompliant, steps = export_budget_replay(EXPORT_BUDGET_PV_ORIGIN, cumulative(pv), cumulative(zero), cumulative(imp), cumulative(zero), 20.0, 20.0)
    failed = check(failed, steps == 12 and close(seed[1], 0.5) and close(noncompliant, 0.0), "pv_origin share {} {}".format(seed, noncompliant))
    # pv_origin: 2 kWh grid charge then 1 kWh exported with no PV -> 1 kWh non-compliant
    seed, noncompliant, steps = export_budget_replay(EXPORT_BUDGET_PV_ORIGIN, cumulative([0.0, 0.0, 0.0]), cumulative([0.0, 0.0, 1.0]), cumulative([1.0, 1.0, 0.0]), cumulative([0.0, 0.0, 0.0]), 20.0, 20.0)
    failed = check(failed, close(noncompliant, 1.0) and close(seed[1], 0.0), "pv_origin non-compliant {} {}".format(seed, noncompliant))
    # pv_equivalent: export beyond bucket + pv is non-compliant
    seed, noncompliant, steps = export_budget_replay(EXPORT_BUDGET_PV_EQUIVALENT, cumulative([0.5, 0.0]), cumulative([0.0, 1.0]), cumulative([0.0, 0.0]), cumulative([0.0, 0.0]), 10.0, 10.0)
    failed = check(failed, close(noncompliant, 0.5) and close(seed[0], 0.0), "pv_equivalent non-compliant {} {}".format(seed, noncompliant))
    # Grid charge 2 kWh, then a sunny step where the battery covers the 0.5 kWh load and all 1 kWh of PV is exported:
    # pv_origin counts the battery output as the house's own use, not as exported grid energy
    history = (cumulative([0.0, 0.0, 1.0]), cumulative([0.0, 0.0, 1.0]), cumulative([1.0, 1.0, 0.0]), cumulative([0.0, 0.0, 0.5]), 20.0, 20.0)
    seed, noncompliant, _ = export_budget_replay(EXPORT_BUDGET_PV_ORIGIN, *history, soc_history=cumulative([1.0, 1.0, -0.5]))
    failed = check(failed, close(noncompliant, 0.0) and close(seed[1], 0.0), "pv_origin battery covers load while PV exports {} {}".format(seed, noncompliant))
    # No history at all
    seed, noncompliant, steps = export_budget_replay(EXPORT_BUDGET_PV_ORIGIN, {}, {}, {}, {}, 10.0, 10.0)
    failed = check(failed, seed is None and steps == 0, "no history")
    return failed


def test_replay_measured_soc():
    """pv_origin with measured SoC history: battery flow is the SoC change, so losses and BMS resyncs cannot create PV energy"""
    failed = False
    zero = [0.0] * 18
    # 6 steps of grid charge (5 kWh), then 12 sunny steps with the battery held: 1 kWh PV, 0.97 exported (3% loss)
    pv = [0.0] * 6 + [1.0] * 12
    imp = [5.0 / 6] * 6 + [0.0] * 12
    exp = [0.0] * 6 + [0.97] * 12
    soc = [5.0 / 6] * 6 + [0.0] * 12
    seed_flow, _, _ = export_budget_replay(EXPORT_BUDGET_PV_ORIGIN, cumulative(pv), cumulative(exp), cumulative(imp), cumulative(zero), 10.0, 10.0)
    seed, noncompliant, steps = export_budget_replay(EXPORT_BUDGET_PV_ORIGIN, cumulative(pv), cumulative(exp), cumulative(imp), cumulative(zero), 10.0, 10.0, soc_history=cumulative(soc))
    # Without SoC history the loss is read as PV charge (the fallback); with it the stored energy stays all grid
    failed = check(failed, seed_flow[1] > 0.01, "flow fallback should show the loss as PV charge {}".format(seed_flow))
    failed = check(failed, steps == 18 and close(seed[1], 0.0) and close(noncompliant, 0.0), "measured SoC phantom charge {} {} {}".format(seed, noncompliant, steps))
    # 2 kWh PV charge, 3 kWh grid charge, then an unexplained 0.3 kWh drop (no load, no export): taken from the PV part first
    pv = [1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
    imp = [0.0, 0.0, 1.0, 1.0, 1.0, 0.0]
    soc = [1.0, 1.0, 1.0, 1.0, 1.0, -0.3]
    zero = [0.0] * 6
    seed, noncompliant, steps = export_budget_replay(EXPORT_BUDGET_PV_ORIGIN, cumulative(pv), cumulative(zero), cumulative(imp), cumulative(zero), 10.0, 10.0, soc_history=cumulative(soc))
    failed = check(failed, close(seed[1], 1.7 / 4.7) and close(noncompliant, 0.0), "measured SoC unexplained drop {} {}".format(seed, noncompliant))
    # Grid charge 2 kWh, then 1 kWh exported from the battery with no PV: non-compliant
    seed, noncompliant, steps = export_budget_replay(EXPORT_BUDGET_PV_ORIGIN, cumulative([0.0] * 3), cumulative([0.0, 0.0, 1.0]), cumulative([1.0, 1.0, 0.0]), cumulative([0.0] * 3), 10.0, 10.0, soc_history=cumulative([1.0, 1.0, -1.0]))
    failed = check(failed, close(noncompliant, 1.0) and close(seed[1], 0.0), "measured SoC non-compliant {} {}".format(seed, noncompliant))
    # SoC history starting part-way: the stored energy at the start counts as grid
    seed, noncompliant, steps = export_budget_replay(EXPORT_BUDGET_PV_ORIGIN, cumulative([1.0]), cumulative([0.0]), cumulative([0.0]), cumulative([0.0]), 10.0, 10.0, soc_history={5: 3.0, 0: 4.0})
    failed = check(failed, close(seed[1], 0.25), "measured SoC initial grid {}".format(seed))
    return failed


def test_replay_skips_counter_glitch():
    """A counter reset pair (+300 then -300 kWh) is skipped, not counted"""
    failed = False
    pv = [1.0, 300.0, -300.0, 1.0]
    exported = [0.0, 0.0, 0.0, 0.0]
    seed, noncompliant, steps = export_budget_replay(EXPORT_BUDGET_PV_EQUIVALENT, cumulative(pv), cumulative(exported), cumulative([0.0] * 4), cumulative([0.0] * 4), 10.0, 7.1)
    failed = check(failed, steps == 2 and close(seed[0], 2.0), "glitch skipped {} {}".format(seed, steps))
    return failed


# Hand-computed cases on a lossless, non-hybrid battery (6 kW, 0.5 kWh per 5 minute step).
# Each row: name, soc_kw, seed (bucket, pv_share), pv_kw by minute range, load_kw, charge windows (start, end, limit),
# export windows (start, end), end_record, and the expected (export_kwh, final_soc) per mode.
BUDGET_CASES = [
    # Grid-charged night then an evening export: pv_origin may only sell the 1 kWh PV part, pv_equivalent the 3 kWh bucket
    {"name": "origin_night_charge", "soc": 2.0, "seed": (3.0, 0.5), "pv": [], "load": 0.0, "charge": [(0, 120, 10.0)], "export": [(180, 240)], "end": 240, "expect": {0: (6.0, 4.0), 1: (1.0, 9.0), 2: (3.0, 7.0)}},
    # Export with house load: house takes the grid part first, export stops when the PV part is gone, battery keeps covering the house
    {"name": "origin_house_cover", "soc": 10.0, "seed": (1.5, 0.2), "pv": [], "load": 1.0, "charge": [], "export": [(0, 60)], "end": 60, "expect": {0: (5.0, 4.0), 1: (2.0, 7.0), 2: (1.5, 7.5)}},
    # Same with the starting SoC overridden (compare.py start_soc): the PV share applies to the new SoC
    {"name": "origin_house_soc_override", "soc": 5.0, "seed": (1.5, 0.2), "pv": [], "load": 1.0, "charge": [], "export": [(0, 60)], "end": 60, "expect": {0: (25.0 / 6.0, 0.0), 1: (1.0, 3.0), 2: (1.5, 2.5)}},
    # PV during a forced export: PV is exported, the empty budget stops the battery adding to it
    {"name": "equivalent_pv_during_export", "soc": 10.0, "seed": (0.0, 0.0), "pv": [(0, 60, 1.2)], "load": 0.0, "charge": [], "export": [(0, 60)], "end": 60, "expect": {0: (7.2, 4.0), 1: (1.2, 10.0), 2: (1.2, 10.0)}},
    # Grid charge window with sun: charge counts as PV up to the step's PV (0.2 of every 0.5), then export
    {"name": "origin_sunny_charge", "soc": 0.0, "seed": (0.0, 0.0), "pv": [(0, 60, 2.4)], "load": 0.0, "charge": [(0, 60, 10.0)], "export": [(60, 120)], "end": 120, "expect": {0: (6.0, 0.0), 1: (2.4, 3.6), 2: (2.4, 3.6)}},
    # Forced export in the sun (PV 2.4 kW, load 1.2 kW) from an all-grid battery: pv_origin lets the battery cover the load
    # while all the PV is exported, but exports nothing from the battery
    {"name": "origin_sunny_export_all_grid", "soc": 10.0, "seed": (0.0, 0.0), "pv": [(0, 60, 2.4)], "load": 1.2, "charge": [], "export": [(0, 60)], "end": 60, "expect": {0: (7.2, 4.0), 1: (2.4, 8.8), 2: (2.4, 8.8)}},
    # Same with 1 kWh of PV stored: the grid part covers the load, so all 2.4 kWh of PV goes out with the 1 kWh
    {"name": "origin_sunny_export_pv_part", "soc": 10.0, "seed": (0.0, 0.1), "pv": [(0, 60, 2.4)], "load": 1.2, "charge": [], "export": [(0, 60)], "end": 60, "expect": {0: (7.2, 4.0), 1: (3.4, 7.8), 2: (2.4, 8.8)}},
]


def setup_budget_case(my_predbat, case):
    """Configure my_predbat for one BUDGET_CASES row, returns the run_prediction arguments"""
    minutes_now = FIXTURE_MINUTES_NOW
    reset_budget_base(my_predbat, minutes_now)
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = case["soc"]
    my_predbat.reserve = 0.0
    my_predbat.best_soc_min = 0.0
    my_predbat.inverter_loss = 1.0
    my_predbat.battery_loss = 1.0
    my_predbat.battery_loss_discharge = 1.0
    my_predbat.inverter_limit = 20 / 60.0
    my_predbat.export_limit = 20 / 60.0
    for attr in ("battery_rate_max_charge", "battery_rate_max_discharge", "battery_rate_max_export", "battery_rate_max_charge_dc", "charge_rate_now", "discharge_rate_now"):
        setattr(my_predbat, attr, 6 / 60.0)
    my_predbat.battery_rate_max_scaling = 1.0
    my_predbat.battery_rate_max_scaling_discharge = 1.0
    my_predbat.battery_rate_min = 0.0
    my_predbat.export_budget_cap_kwh = 10.0
    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_kw = 0.0
        for start, end, kw in case["pv"]:
            if start <= minute < end:
                pv_kw = kw
        pv_step[minute] = pv_kw / 12.0
        load_step[minute] = case["load"] / 12.0
    charge_window = [{"start": minutes_now + start, "end": minutes_now + end, "average": 10.0} for start, end, _ in case["charge"]]
    charge_limit = [limit for _, _, limit in case["charge"]]
    export_window = [{"start": minutes_now + start, "end": minutes_now + end, "average": 50.0} for start, end in case["export"]]
    export_limits = [unpack_export_limit(0.0) for _ in case["export"]]
    return {"pv_step": pv_step, "load_step": load_step, "charge_limit": charge_limit, "charge_window": charge_window, "export_window": export_window, "export_limits": export_limits, "end_record": case["end"]}


def run_budget_case_python(my_predbat, case, mode, step=5, save=None):
    """Run one BUDGET_CASES row through the Python engine with the given mode, returns (result, prediction)"""
    args = setup_budget_case(my_predbat, case)
    my_predbat.export_budget_mode = mode
    my_predbat.export_budget_seed = case["seed"] if mode else None
    my_predbat.prediction_kernel_enable = False
    pv, load = args["pv_step"], args["load_step"]
    prediction = Prediction(my_predbat, pv, pv, load, load, pv, load)
    result = prediction.run_prediction(args["charge_limit"], args["charge_window"], args["export_window"], args["export_limits"], PV_SCENARIO_NOMINAL, args["end_record"], save=save, step=step, cache=False)
    return result, prediction


def test_budget_engine_cases(my_predbat):
    """Hand-computed export and final SoC for every case and mode"""
    failed = False
    for case in BUDGET_CASES:
        for mode, (expect_export, expect_soc) in case["expect"].items():
            result, _ = run_budget_case_python(my_predbat, case, mode)
            export_kwh, final_soc = result[3], result[5]
            # Results are rounded to 4 decimal places
            if abs(export_kwh - expect_export) > 1e-3 or abs(final_soc - expect_soc) > 1e-3:
                print("ERROR: export_budget case {} mode {}: export {} soc {} expected export {} soc {}".format(case["name"], mode, export_kwh, final_soc, expect_export, expect_soc))
                failed = True
    return failed


def test_budget_parts_invariant(my_predbat):
    """pv_origin's PV part stays within [0, soc_max] and the bucket within [0, cap] across lossy random runs"""
    import random
    from tests.test_kernel_parity import apply_random_scenario, make_step_data, make_windows
    from tests.test_infra import reset_inverter, reset_rates

    failed = False
    # The fixture has one car; random scenarios use up to two, so widen the car lists for this test only
    saved_car_lists = {attr: value for attr, value in vars(my_predbat).items() if attr.startswith("car_charging") and isinstance(value, list) and len(value) == 1}
    for attr, value in saved_car_lists.items():
        setattr(my_predbat, attr, value + [copy.deepcopy(value[0]) for _ in range(3)])
    for seed in range(20):
        rng = random.Random(seed + 4000000)
        reset_inverter(my_predbat)
        reset_rates(my_predbat, 10.0, 5.0)
        my_predbat.battery_rate_max_export = my_predbat.battery_rate_max_discharge
        apply_random_scenario(my_predbat, rng)
        pv_step, pv10_step, load_step, load10_step = make_step_data(my_predbat, rng=rng)
        charge_window = make_windows(rng, my_predbat.minutes_now, my_predbat.forecast_minutes, 2)
        charge_limit = [my_predbat.soc_max for _ in charge_window]
        export_window = make_windows(rng, my_predbat.minutes_now, my_predbat.forecast_minutes, 2)
        export_limits = [unpack_export_limit(0.0) for _ in export_window]
        for mode in (1, 2):
            my_predbat.export_budget_mode = mode
            my_predbat.export_budget_cap_kwh = 0.0
            my_predbat.export_budget_seed = (rng.uniform(0, my_predbat.soc_max), rng.uniform(0, 1))
            my_predbat.prediction_kernel_enable = False
            prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step)
            prediction.run_prediction(charge_limit, charge_window, export_window, export_limits, PV_SCENARIO_NOMINAL, my_predbat.forecast_minutes, save="best", cache=False)
            for minute, value in prediction.predict_export_budget.items():
                if value < -1e-9 or value > my_predbat.soc_max + 1e-9:
                    print("ERROR: export_budget invariant seed {} mode {} minute {} value {} soc_max {}".format(seed, mode, minute, value, my_predbat.soc_max))
                    failed = True
                    break
    for attr, value in saved_car_lists.items():
        setattr(my_predbat, attr, value)
    my_predbat.export_budget_mode = 0
    my_predbat.export_budget_seed = None
    return failed


def test_budget_fast_mode_step(my_predbat):
    """The cap holds with 10 minute steps (Python fast mode, kernel unavailable)"""
    case = BUDGET_CASES[1]
    result, _ = run_budget_case_python(my_predbat, case, 1, step=10)
    if not (0 < result[3] <= 2.0 + 1e-6):
        print("ERROR: export_budget fast mode exported {} (budget 2.0)".format(result[3]))
        return True
    return False


def test_budget_save_outputs(my_predbat):
    """save='best' records capped minutes and the planned battery export"""
    case = BUDGET_CASES[1]
    _, prediction = run_budget_case_python(my_predbat, case, 1, save="best")
    failed = False
    if abs(prediction.export_budget_planned_battery_export - 2.0) > 1e-6:
        print("ERROR: export_budget planned battery export {} expected 2.0".format(prediction.export_budget_planned_battery_export))
        failed = True
    if not prediction.export_budget_capped_minutes:
        print("ERROR: export_budget capped minutes empty for a capped export")
        failed = True
    return failed


def test_config_parse(my_predbat):
    """apps.yaml parsing: valid modes, invalid mode -> off with Warn, invalid cap -> default"""
    failed = False
    for value, expect_mode in (("pv_origin", 1), ("pv_equivalent", 2), ("off", 0), ("bogus", 0)):
        my_predbat.args["export_budget_mode"] = value
        my_predbat.args["export_budget_cap_kwh"] = "abc" if value == "bogus" else 7.1
        my_predbat.fetch_config_options()
        if my_predbat.export_budget_mode != expect_mode:
            print("ERROR: export_budget config {} parsed as {} expected {}".format(value, my_predbat.export_budget_mode, expect_mode))
            failed = True
        expect_cap = 0.0 if value == "bogus" else 7.1
        if my_predbat.export_budget_cap_kwh != expect_cap:
            print("ERROR: export_budget cap for {} parsed as {} expected {}".format(value, my_predbat.export_budget_cap_kwh, expect_cap))
            failed = True
    my_predbat.args.pop("export_budget_mode", None)
    my_predbat.args.pop("export_budget_cap_kwh", None)
    # `export_budget_mode: off` unquoted is YAML false: it means off and must pass apps.yaml validation
    my_predbat.args["export_budget_mode"] = False
    saved_arg_errors = my_predbat.arg_errors
    my_predbat.arg_errors = {}
    try:
        my_predbat.validate_config()
        if "export_budget_mode" in my_predbat.arg_errors:
            print("ERROR: export_budget_mode false failed validation: {}".format(my_predbat.arg_errors["export_budget_mode"]))
            failed = True
    finally:
        my_predbat.arg_errors = saved_arg_errors
        my_predbat.args.pop("export_budget_mode", None)
    my_predbat.fetch_config_options()
    return failed


def test_seed_hook(my_predbat):
    """update_export_budget_seed replays the loaded history, and is a no-op with the mode off"""
    failed = False
    n = 12
    my_predbat.pv_today = cumulative([1.0] * n)
    my_predbat.export_today = cumulative([0.2] * n)
    my_predbat.import_today = cumulative([0.0] * n)
    my_predbat.load_minutes = cumulative([0.0] * n)
    my_predbat.soc_max = 10.0
    my_predbat.export_budget_cap_kwh = 0.0
    my_predbat.export_budget_mode = 2
    my_predbat.update_export_budget_seed()
    if not my_predbat.export_budget_seed or abs(my_predbat.export_budget_seed[0] - 9.6) > 1e-9 or my_predbat.export_budget_replay_steps != n:
        print("ERROR: export_budget seed hook gave {} steps {}".format(my_predbat.export_budget_seed, my_predbat.export_budget_replay_steps))
        failed = True
    my_predbat.export_budget_mode = 0
    my_predbat.update_export_budget_seed()
    if my_predbat.export_budget_seed is not None:
        print("ERROR: export_budget seed hook with mode off left {}".format(my_predbat.export_budget_seed))
        failed = True
    return failed


def test_soc_history_fetch(my_predbat):
    """The SoC history comes from each inverter's soc_percent (or soc_kw) sensor, in stored kWh keyed by minutes ago, summed"""
    from datetime import timedelta

    failed = False
    now = my_predbat.now_utc
    records = [{"last_updated": (now - timedelta(minutes=60)).isoformat(), "state": "50"}, {"last_updated": (now - timedelta(minutes=30)).isoformat(), "state": "60"}]
    saved_soc_max = [inverter.__dict__.get("soc_max", "missing") for inverter in my_predbat.inverters]
    saved_scaling = [inverter.__dict__.get("battery_scaling", "missing") for inverter in my_predbat.inverters]
    for inverter in my_predbat.inverters:
        inverter.soc_max = 10.0
    my_predbat.args["soc_percent"] = "sensor.test_battery_soc"
    my_predbat.args.pop("soc_kw", None)
    my_predbat.get_history_wrapper = lambda entity_id, days=30, required=True, tracked=True: [records]
    try:
        history = my_predbat.fetch_export_budget_soc_history()
        total = 10.0 * len(my_predbat.inverters)
        if not history or abs(history.get(0, -1) - 0.6 * total) > 1e-6 or abs(history.get(45, -1) - 0.5 * total) > 1e-6:
            print("ERROR: export_budget SoC history now {} 45 minutes ago {} expected {} / {}".format(history.get(0) if history else None, history.get(45) if history else None, 0.6 * total, 0.5 * total))
            failed = True
        # A soc_kw sensor is scaled by battery_scaling, as the live SoC is (inverter.py)
        my_predbat.args.pop("soc_percent", None)
        my_predbat.args["soc_kw"] = "sensor.test_battery_soc_kw"
        for inverter in my_predbat.inverters:
            inverter.battery_scaling = 0.8
        kw_records = [{"last_updated": (now - timedelta(minutes=60)).isoformat(), "state": "5"}, {"last_updated": (now - timedelta(minutes=30)).isoformat(), "state": "6"}]
        my_predbat.get_history_wrapper = lambda entity_id, days=30, required=True, tracked=True: [kw_records]
        history = my_predbat.fetch_export_budget_soc_history()
        expect_now, expect_45 = 0.8 * 6 * len(my_predbat.inverters), 0.8 * 5 * len(my_predbat.inverters)
        if not history or abs(history.get(0, -1) - expect_now) > 1e-6 or abs(history.get(45, -1) - expect_45) > 1e-6:
            print("ERROR: export_budget soc_kw history now {} 45 minutes ago {} expected {} / {}".format(history.get(0) if history else None, history.get(45) if history else None, expect_now, expect_45))
            failed = True
        my_predbat.get_history_wrapper = lambda entity_id, days=30, required=True, tracked=True: []
        if my_predbat.fetch_export_budget_soc_history():
            print("ERROR: export_budget SoC history should be empty without sensor history")
            failed = True
    finally:
        del my_predbat.get_history_wrapper
        my_predbat.args.pop("soc_kw", None)
        my_predbat.args["soc_percent"] = "sensor.test_battery_soc"
        for inverter, soc_max, scaling in zip(my_predbat.inverters, saved_soc_max, saved_scaling):
            if soc_max == "missing":
                del inverter.soc_max
            else:
                inverter.soc_max = soc_max
            if scaling == "missing":
                inverter.__dict__.pop("battery_scaling", None)
            else:
                inverter.battery_scaling = scaling
    return failed


def test_seed_hook_measured_soc(my_predbat):
    """The pv_origin seed uses the measured SoC history when there is one, and falls back to the flow estimate otherwise"""
    failed = False
    pv = [0.0] * 6 + [1.0] * 12
    my_predbat.pv_today = cumulative(pv)
    my_predbat.export_today = cumulative([0.0] * 6 + [0.97] * 12)
    my_predbat.import_today = cumulative([5.0 / 6] * 6 + [0.0] * 12)
    my_predbat.load_minutes = cumulative([0.0] * 18)
    my_predbat.soc_max = 10.0
    my_predbat.export_budget_cap_kwh = 0.0
    my_predbat.export_budget_mode = 1
    soc = cumulative([5.0 / 6] * 6 + [0.0] * 12)
    for history, expect_pv in ((soc, False), ({}, True)):
        my_predbat.fetch_export_budget_soc_history = lambda history=history: history
        try:
            my_predbat.update_export_budget_seed()
        finally:
            del my_predbat.fetch_export_budget_soc_history
        share = my_predbat.export_budget_seed[1] if my_predbat.export_budget_seed else None
        if share is None or (share > 0.01) != expect_pv:
            print("ERROR: export_budget seed hook with SoC history {} gave share {}".format(bool(history), share))
            failed = True
    my_predbat.export_budget_mode = 0
    return failed


def test_publish_sensor(my_predbat):
    """The sensor carries the agreed state and attributes and the manual export note is logged"""
    failed = False
    my_predbat.export_budget_mode = 1
    my_predbat.export_budget_cap_kwh = 0.0
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 8.0
    my_predbat.export_budget_seed = (0.0, 0.25)
    my_predbat.export_budget_noncompliant_kwh = 0.4
    my_predbat.export_budget_replay_steps = 2016
    my_predbat.export_budget_planned_battery_export = 1.5
    start = my_predbat.minutes_now + 60
    other = start + 120
    my_predbat.export_window_best = [{"start": start, "end": start + 30, "average": 50}, {"start": other, "end": other + 30, "average": 50}]
    # Only the first window is manual; both are capped, so exactly one manual note is expected
    my_predbat.manual_export_times = [start]
    my_predbat.export_budget_capped_minutes = [start + 10, other + 5]
    # Budget at the end of each 5 minute step of the best plan, keyed by minutes from now
    my_predbat.predict_export_budget = {0: 1.8, 5: 1.5, 10: 1.5, 15: 1.5, 20: 3.0}
    logged = []
    saved_log = my_predbat.log
    my_predbat.log = lambda message, *args, **kwargs: logged.append(message)
    try:
        my_predbat.publish_export_budget()
    finally:
        my_predbat.log = saved_log
    entity = my_predbat.prefix + ".export_budget"
    item = my_predbat.dashboard_values.get(entity, {})
    state = item.get("state")
    attributes = item.get("attributes", {})
    if state is None or abs(float(state) - 2.0) > 1e-6:
        print("ERROR: export_budget sensor state {} expected 2.0".format(state))
        failed = True
    for key, value in (("mode", "pv_origin"), ("pv_part_kwh", 2.0), ("grid_part_kwh", 6.0), ("cap", 10.0), ("planned_battery_export_kwh", 1.5), ("noncompliant_export_kwh", 0.4)):
        if attributes.get(key) != value:
            print("ERROR: export_budget attribute {} = {} expected {}".format(key, attributes.get(key), value))
            failed = True

    def stamp(minutes_from_now):
        return (my_predbat.midnight_utc + timedelta(minutes=my_predbat.minutes_now + minutes_from_now)).strftime(TIME_FORMAT)

    # Starts at the budget now; repeated values in the middle of a run are dropped, as for other forecast sensors
    expected_results = {stamp(0): 2.0, stamp(5): 1.8, stamp(10): 1.5, stamp(20): 1.5, stamp(25): 3.0}
    if attributes.get("results") != expected_results:
        print("ERROR: export_budget results {} expected {}".format(attributes.get("results"), expected_results))
        failed = True
    manual_notes = [message for message in logged if "capped manual export" in message]
    if len(manual_notes) != 1 or my_predbat.time_abs_str(start) not in manual_notes[0]:
        print("ERROR: export_budget manual export notes {} expected one for {}".format(manual_notes, my_predbat.time_abs_str(start)))
        failed = True
    if not any(message.startswith("Export energy budget (pv_origin)") for message in logged):
        print("ERROR: export_budget summary log line missing: {}".format(logged))
        failed = True
    return failed


def test_budget_exportable_output(my_predbat):
    """save runs record the stored energy the budget lets the battery export at the start of each step"""
    failed = False
    case = BUDGET_CASES[1]
    # Seed (1.5, 0.2) with 10 kWh stored: pv_origin may export the 2 kWh PV part, pv_equivalent the 1.5 kWh bucket
    for mode, expect in ((1, 2.0), (2, 1.5)):
        _, prediction = run_budget_case_python(my_predbat, case, mode, save="best")
        value = prediction.predict_export_budget_start.get(0)
        if value is None or abs(value - expect) > 1e-6:
            print("ERROR: export_budget exportable at start mode {} = {} expected {}".format(mode, value, expect))
            failed = True
    return failed


def test_clip_budget_floor(my_predbat):
    """In budget mode an export target is never below start SoC minus the exportable budget, whatever the house load does"""
    from const import EXPORT_MODE_TARGET
    from utils import pack_export_limit, export_target_of

    failed = False
    reset_budget_base(my_predbat, FIXTURE_MINUTES_NOW)
    my_predbat.soc_max = 10.0
    my_predbat.debug_enable = False
    my_predbat.battery_rate_max_discharge = 1 / 60.0
    my_predbat.battery_rate_max_scaling_discharge = 1.0
    minutes_now = FIXTURE_MINUTES_NOW
    # Simulated SoC falls 8 -> 5 kWh over the window (export plus the house load)
    predict_soc = {minute: 8.0 - 3.0 * min(minute, 30) / 30.0 for minute in range(0, 65, 5)}
    # Uncapped clip: display target at the lowest SoC (50%), command 10 minutes of discharge below it (48%)
    for mode, exportable, expect_limit, expect_target in ((1, 0.2, 78, 78), (1, 5.0, 48, 50), (0, 0.2, 48, 50)):
        my_predbat.export_budget_mode = mode
        windows = [{"start": minutes_now, "end": minutes_now + 30, "average": 50.0}]
        limits = [pack_export_limit(EXPORT_MODE_TARGET, 0, 1.0)]
        _, limits = my_predbat.clip_export_slots(minutes_now, predict_soc, windows, limits, 1, 5, budget_exportable={0: exportable})
        if export_target_of(limits[0]) != expect_limit or windows[0]["target"] != expect_target:
            print("ERROR: export_budget clip floor mode {} exportable {} gave target {} window {} expected {} / {}".format(mode, exportable, export_target_of(limits[0]), windows[0]["target"], expect_limit, expect_target))
            failed = True
    my_predbat.export_budget_mode = 0
    return failed


def test_measured_floor(my_predbat):
    """Every cycle the export window running now (or starting before the next cycle) is held at or above measured SoC minus the budget left now"""
    from const import EXPORT_MODE_TARGET, EXPORT_MODE_FREEZE
    from utils import pack_export_limit, export_target_of, export_mode_of

    failed = False
    minutes_now = FIXTURE_MINUTES_NOW
    my_predbat.minutes_now = minutes_now
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 5.0
    my_predbat.battery_loss_discharge = 1.0
    my_predbat.inverter_loss = 1.0
    # mode, seed, expected target of the running window: pv_origin 20% of 5 kWh -> 1 kWh exportable -> 40%;
    # pv_equivalent bucket 2 kWh -> 30%; off -> unchanged 10%; no history -> nothing exportable -> 50%
    for mode, seed, expect in ((1, (0.0, 0.2), 40), (2, (2.0, 0.0), 30), (0, (0.0, 0.2), 10), (1, None, 50)):
        my_predbat.export_budget_mode = mode
        my_predbat.export_budget_seed = seed
        my_predbat.export_window_best = [
            {"start": minutes_now - 30, "end": minutes_now + 30, "average": 50.0, "target": 10.0},
            {"start": minutes_now + 3, "end": minutes_now + 30, "average": 50.0, "target": 10.0},
            {"start": minutes_now + 120, "end": minutes_now + 150, "average": 50.0, "target": 10.0},
            {"start": minutes_now - 5, "end": minutes_now + 30, "average": 50.0, "target": 99.0},
        ]
        my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 10, 1.0), pack_export_limit(EXPORT_MODE_TARGET, 10, 1.0), pack_export_limit(EXPORT_MODE_TARGET, 10, 1.0), pack_export_limit(EXPORT_MODE_FREEZE)]
        my_predbat.apply_export_budget_floor()
        targets = [export_target_of(limit) for limit in my_predbat.export_limits_best[:3]]
        if targets != [expect, expect, 10] or my_predbat.export_window_best[0]["target"] != expect:
            print("ERROR: export_budget measured floor mode {} seed {} gave {} (window {}) expected [{}, {}, 10]".format(mode, seed, targets, my_predbat.export_window_best[0]["target"], expect, expect))
            failed = True
        if export_mode_of(my_predbat.export_limits_best[3]) != EXPORT_MODE_FREEZE:
            print("ERROR: export_budget measured floor changed a freeze window: {}".format(my_predbat.export_limits_best[3]))
            failed = True
    my_predbat.export_budget_mode = 0
    my_predbat.export_budget_seed = None
    return failed


def test_budget_iboost_excess(my_predbat):
    """iBoost solar-excess diversion only sees the export left after the cap, so the cap never makes the house import"""
    failed = False
    case = BUDGET_CASES[1]
    iboost = {
        "iboost_enable": True,
        "iboost_solar": True,
        "iboost_solar_excess": True,
        "iboost_on_export": True,
        "iboost_max_power": 3 / 60.0,
        "iboost_max_energy": 100.0,
        "iboost_min_power": 0.0,
        "iboost_min_soc": 0.0,
        "iboost_rate_threshold": 9999.0,
        "iboost_rate_threshold_export": 9999.0,
        "iboost_today": 0.0,
    }
    saved = {attr: getattr(my_predbat, attr, None) for attr in iboost}
    try:
        for mode in (1, 2):
            args = setup_budget_case(my_predbat, case)
            for attr, value in iboost.items():
                setattr(my_predbat, attr, value)
            my_predbat.export_budget_mode = mode
            my_predbat.export_budget_seed = case["seed"]
            my_predbat.prediction_kernel_enable = False
            pv, load = args["pv_step"], args["load_step"]
            prediction = Prediction(my_predbat, pv, pv, load, load, pv, load)
            result = prediction.run_prediction(args["charge_limit"], args["charge_window"], args["export_window"], args["export_limits"], PV_SCENARIO_NOMINAL, args["end_record"], save=None, cache=False)
            if result[2] > 1e-6:
                print("ERROR: export_budget with iBoost solar excess mode {} imported {} kWh for the house".format(mode, result[2]))
                failed = True
    finally:
        for attr, value in saved.items():
            setattr(my_predbat, attr, value)
    return failed


def run_export_budget_tests(my_predbat):
    """Run all export budget tests, returns True on failure"""
    print("**** Running export budget tests ****")
    failed = False
    failed |= test_parse_mode()
    failed |= test_allowed()
    failed |= test_house_from_battery()
    failed |= test_update_pv_equivalent()
    failed |= test_update_pv_origin()
    failed |= test_replay()
    failed |= test_replay_skips_counter_glitch()
    failed |= test_replay_measured_soc()
    from tests.test_kernel_parity import snapshot_scenario_state, restore_scenario_state

    state = snapshot_scenario_state(my_predbat)
    try:
        failed |= test_budget_engine_cases(my_predbat)
        failed |= test_budget_parts_invariant(my_predbat)
        failed |= test_budget_fast_mode_step(my_predbat)
        failed |= test_budget_save_outputs(my_predbat)
        failed |= test_budget_exportable_output(my_predbat)
        failed |= test_budget_iboost_excess(my_predbat)
        failed |= test_clip_budget_floor(my_predbat)
        # Config parsing and the seed hook mutate the args and the loaded history
        saved = {
            attr: copy.deepcopy(getattr(my_predbat, attr, None))
            for attr in (
                "pv_today",
                "export_today",
                "import_today",
                "load_minutes",
                "soc_max",
                "args",
                "export_budget_noncompliant_kwh",
                "export_budget_replay_steps",
                "export_budget_planned_battery_export",
                "export_budget_capped_minutes",
                "soc_kw",
                "export_window_best",
                "manual_export_times",
                "export_limits_best",
                "minutes_now",
                "battery_loss_discharge",
                "inverter_loss",
            )
        }
        try:
            failed |= test_config_parse(my_predbat)
            failed |= test_seed_hook(my_predbat)
            failed |= test_soc_history_fetch(my_predbat)
            failed |= test_seed_hook_measured_soc(my_predbat)
            failed |= test_publish_sensor(my_predbat)
            failed |= test_measured_floor(my_predbat)
        finally:
            for attr, value in saved.items():
                setattr(my_predbat, attr, value)
    finally:
        restore_scenario_state(my_predbat, state)
    if not failed:
        print("PASS")
    return failed
