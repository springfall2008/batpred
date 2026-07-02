# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Parity tests for the C++ prediction kernel.

Every scenario is run twice on identical inputs - once through the Python
engine (Prediction.run_prediction) and once through the C++ kernel - and the
full result tuples (including the per-step SoC prediction) must match to
within 1e-6. A deterministic edge-case table pins each kernel branch and a
seeded random sweep covers the wider configuration space.

If the kernel shared library is not present the test attempts to build it with
build_kernel.sh; if that fails the test is skipped with a loud notice, unless
PREDBAT_KERNEL_REQUIRED=1 is set (CI) in which case it fails.
"""

import copy
import os
import random
import subprocess

import prediction_kernel
from prediction import Prediction
from prediction_kernel import create_kernel_context, run_prediction_kernel, load_kernel
from tests.test_infra import reset_inverter, reset_rates
from tests.test_model import run_model_tests

# Tolerance for scalar and SoC comparisons; the kernel targets bit-exact results
PARITY_TOLERANCE = 1e-6

RESULT_NAMES = [
    "final_metric",
    "import_kwh_battery",
    "import_kwh_house",
    "export_kwh",
    "soc_min",
    "final_soc",
    "soc_min_minute",
    "battery_cycle",
    "metric_keep",
    "final_iboost",
    "final_carbon_g",
]

# Attributes mutated by the parity scenarios that reset_inverter/reset_rates do not restore;
# snapshotted before the tests and restored afterwards so later tests see a clean predbat
SCENARIO_STATE_ATTRS = [
    "soc_max",
    "soc_kw",
    "reserve",
    "best_soc_min",
    "best_soc_keep",
    "best_soc_keep_weight",
    "battery_rate_max_charge",
    "battery_rate_max_charge_dc",
    "battery_rate_max_discharge",
    "battery_rate_max_export",
    "battery_rate_min",
    "charge_rate_now",
    "discharge_rate_now",
    "battery_rate_max_scaling",
    "battery_rate_max_scaling_discharge",
    "charge_scaling10",
    "battery_loss",
    "battery_loss_discharge",
    "inverter_hybrid",
    "inverter_loss",
    "inverter_limit",
    "export_limit",
    "pv_ac_limit",
    "inverter_can_charge_during_export",
    "set_charge_freeze",
    "set_reserve_enable",
    "set_export_freeze",
    "set_export_freeze_only",
    "set_charge_window",
    "set_export_window",
    "set_discharge_during_charge",
    "set_export_low_power",
    "calculate_export_on_pv",
    "battery_charge_power_curve",
    "battery_discharge_power_curve",
    "battery_temperature",
    "battery_temperature_prediction",
    "battery_temperature_charge_curve",
    "battery_temperature_discharge_curve",
    "rate_max",
    "rate_import",
    "rate_export",
    "io_adjusted",
    "all_active_keep",
    "carbon_enable",
    "carbon_intensity",
    "carbon_today_sofar",
    "num_cars",
    "car_charging_loss",
    "car_energy_reported_load",
    "car_charging_from_battery",
    "car_charging_soc",
    "car_charging_limit",
    "car_charging_slots",
    "iboost_enable",
    "iboost_solar",
    "iboost_solar_excess",
    "iboost_charging",
    "iboost_gas",
    "iboost_gas_export",
    "iboost_prevent_discharge",
    "iboost_on_export",
    "iboost_max_energy",
    "iboost_max_power",
    "iboost_min_power",
    "iboost_min_soc",
    "iboost_rate_threshold",
    "iboost_rate_threshold_export",
    "iboost_gas_scale",
    "iboost_today",
    "rate_gas",
    "iboost_plan",
    "end_record",
]


def snapshot_scenario_state(my_predbat):
    """Deep-copy the predbat attributes the parity scenarios mutate"""
    return {attr: copy.deepcopy(getattr(my_predbat, attr)) for attr in SCENARIO_STATE_ATTRS if hasattr(my_predbat, attr)}


def restore_scenario_state(my_predbat, state):
    """Restore attributes captured by snapshot_scenario_state"""
    for attr, value in state.items():
        setattr(my_predbat, attr, value)


def ensure_kernel_built():
    """Ensure a loadable kernel shared library is available, building one locally if necessary.

    Existence alone isn't enough to skip building - a candidate binary (e.g. a checked-in
    cross-built one) can be stale (parity revision mismatch) or corrupted - so this actually
    attempts to load before deciding a build is needed. Returns True once a usable library
    has been loaded, whether that was an existing candidate or a freshly built local one.
    """
    prediction_kernel.KERNEL_LOAD_TRIED = False
    prediction_kernel.KERNEL_LIB = None
    if load_kernel(log=print):
        return True

    build_script = os.path.join(os.path.dirname(os.path.abspath(prediction_kernel.__file__)), "build_kernel.sh")
    try:
        result = subprocess.run(["bash", build_script], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print("Kernel build failed: {}".format(result.stderr))
            return False
    except (OSError, subprocess.SubprocessError) as error:
        print("Kernel build failed: {}".format(error))
        return False

    prediction_kernel.KERNEL_LOAD_TRIED = False
    prediction_kernel.KERNEL_LIB = None
    return load_kernel(log=print) is not None


def make_windows(rng, minutes_now, forecast_minutes, count, align=5):
    """Generate a list of random windows within the forecast horizon"""
    windows = []
    for _ in range(count):
        start = minutes_now + rng.randrange(0, forecast_minutes - 60, align)
        length = rng.randrange(30, 8 * 60, align)
        windows.append({"start": start, "end": min(start + length, minutes_now + forecast_minutes), "average": round(rng.uniform(0, 40), 2)})
    return windows


def apply_random_scenario(my_predbat, rng):
    """Randomise my_predbat's battery/inverter/rate configuration for one parity scenario"""
    soc_max = rng.choice([4.8, 9.5, 10.0, 19.0, 50.0, 100.0])
    my_predbat.soc_max = soc_max
    my_predbat.soc_kw = round(rng.uniform(0, soc_max), 2)
    my_predbat.reserve = rng.choice([0.0, round(soc_max * 0.04, 2), round(rng.uniform(0, soc_max / 4), 2)])
    my_predbat.best_soc_min = rng.choice([0.0, my_predbat.reserve])
    my_predbat.best_soc_keep = rng.choice([0.0, round(rng.uniform(0, soc_max / 2), 2)])
    my_predbat.best_soc_keep_weight = round(rng.uniform(0, 1), 2)

    my_predbat.battery_rate_max_charge = rng.uniform(0.5, 5) / 60.0
    my_predbat.battery_rate_max_charge_dc = my_predbat.battery_rate_max_charge * rng.choice([1.0, 1.0, 1.5, 2.0])
    my_predbat.battery_rate_max_discharge = rng.uniform(0.5, 5) / 60.0
    my_predbat.battery_rate_max_export = my_predbat.battery_rate_max_discharge * rng.choice([1.0, 1.0, 0.5])
    my_predbat.battery_rate_min = rng.choice([0.0, 0.0, 0.05 / 60.0])
    my_predbat.charge_rate_now = my_predbat.battery_rate_max_charge * rng.choice([1.0, 0.5])
    my_predbat.discharge_rate_now = my_predbat.battery_rate_max_discharge * rng.choice([1.0, 0.5])
    my_predbat.battery_rate_max_scaling = rng.choice([1.0, round(rng.uniform(0.9, 1.0), 3)])
    my_predbat.battery_rate_max_scaling_discharge = rng.choice([1.0, round(rng.uniform(0.9, 1.0), 3)])
    my_predbat.charge_scaling10 = rng.choice([1.0, round(rng.uniform(0.8, 1.0), 3)])
    my_predbat.battery_loss = round(rng.uniform(0.9, 1.0), 3)
    my_predbat.battery_loss_discharge = round(rng.uniform(0.9, 1.0), 3)

    my_predbat.inverter_hybrid = rng.choice([True, False])
    my_predbat.inverter_loss = round(rng.uniform(0.8, 1.0), 3)
    my_predbat.inverter_limit = rng.uniform(1, 10) / 60.0
    my_predbat.export_limit = rng.uniform(0, 10) / 60.0
    my_predbat.pv_ac_limit = rng.choice([0.0, rng.uniform(1, 5) / 60.0])
    my_predbat.inverter_can_charge_during_export = rng.choice([True, False])

    my_predbat.set_charge_freeze = rng.choice([True, False])
    my_predbat.set_reserve_enable = rng.choice([True, False])
    my_predbat.set_export_freeze = rng.choice([True, False])
    my_predbat.set_export_freeze_only = rng.choice([True, False, False, False])
    my_predbat.set_charge_window = rng.choice([True, True, False])
    my_predbat.set_export_window = rng.choice([True, True, False])
    my_predbat.set_discharge_during_charge = rng.choice([True, False])
    my_predbat.set_export_low_power = rng.choice([True, False])
    my_predbat.calculate_export_on_pv = rng.choice([True, False])

    # Random battery power curves (percent -> multiplier), sometimes empty
    if rng.random() < 0.5:
        my_predbat.battery_charge_power_curve = {percent: round(rng.uniform(0.05, 1.0), 2) for percent in range(rng.randint(90, 100), 101)}
    else:
        my_predbat.battery_charge_power_curve = {}
    if rng.random() < 0.5:
        my_predbat.battery_discharge_power_curve = {percent: round(rng.uniform(0.05, 1.0), 2) for percent in range(rng.randint(90, 100), 101)}
    else:
        my_predbat.battery_discharge_power_curve = {}

    # Battery temperature model
    my_predbat.battery_temperature = rng.choice([20, 15, 8, 3, 0, -5])
    my_predbat.battery_temperature_prediction = {minute: max(my_predbat.battery_temperature - minute / (4 * 60.0), -10) for minute in range(0, my_predbat.forecast_minutes, 5)} if rng.random() < 0.5 else {}

    # Random rates in 30 minute blocks, occasional negative export/import rates
    for minute in range(0, my_predbat.forecast_minutes + my_predbat.minutes_now, 30):
        import_rate = round(rng.uniform(-5, 45), 2)
        export_rate = round(rng.uniform(0, 30), 2)
        for offset in range(30):
            my_predbat.rate_import[minute + offset] = import_rate
            my_predbat.rate_export[minute + offset] = export_rate
    my_predbat.rate_max = max(my_predbat.rate_import.values())

    # Octopus intelligent adjusted slots and alerts
    my_predbat.io_adjusted = {}
    if rng.random() < 0.3:
        start = my_predbat.minutes_now + rng.randrange(0, my_predbat.forecast_minutes - 60, 5)
        for minute in range(start, start + 60):
            my_predbat.io_adjusted[minute] = True
    my_predbat.all_active_keep = {}
    if rng.random() < 0.3:
        start = my_predbat.minutes_now + rng.randrange(0, my_predbat.forecast_minutes - 60, 5)
        for minute in range(start, start + 120):
            my_predbat.all_active_keep[minute] = rng.choice([20, 50, 100])

    # Carbon intensity
    my_predbat.carbon_enable = rng.random() < 0.3
    my_predbat.carbon_intensity = {minute: round(rng.uniform(0, 400), 1) for minute in range(0, my_predbat.forecast_minutes, 5)} if my_predbat.carbon_enable else {}
    my_predbat.carbon_today_sofar = round(rng.uniform(0, 2000), 1) if my_predbat.carbon_enable else 0

    # Cars
    my_predbat.num_cars = rng.choice([0, 0, 0, 1, 1, 2])
    my_predbat.car_charging_loss = round(rng.uniform(0.85, 1.0), 3)
    my_predbat.car_energy_reported_load = rng.choice([True, False])
    my_predbat.car_charging_from_battery = rng.choice([True, False])
    for car_n in range(my_predbat.num_cars):
        my_predbat.car_charging_soc[car_n] = round(rng.uniform(0, 30), 2)
        my_predbat.car_charging_limit[car_n] = round(rng.uniform(20, 80), 2)
        slots = []
        for _ in range(rng.randint(0, 3)):
            start = my_predbat.minutes_now + rng.randrange(0, my_predbat.forecast_minutes - 60, 30)
            length = rng.randrange(30, 4 * 60, 30)
            slots.append({"start": start, "end": start + length, "kwh": round(rng.uniform(1, 20), 2), "average": round(rng.uniform(5, 40), 2), "octopus": rng.choice([True, False])})
        my_predbat.car_charging_slots[car_n] = slots

    # iBoost
    my_predbat.iboost_enable = rng.random() < 0.4
    if my_predbat.iboost_enable:
        my_predbat.iboost_solar = rng.choice([True, False])
        my_predbat.iboost_solar_excess = rng.choice([True, False])
        my_predbat.iboost_charging = rng.choice([True, False])
        my_predbat.iboost_gas = rng.choice([True, False])
        my_predbat.iboost_gas_export = rng.choice([True, False])
        my_predbat.iboost_prevent_discharge = rng.choice([True, False])
        my_predbat.iboost_on_export = rng.choice([True, False])
        my_predbat.iboost_max_energy = round(rng.uniform(1, 20), 2)
        my_predbat.iboost_max_power = rng.uniform(1, 4) / 60.0
        my_predbat.iboost_min_power = rng.choice([0.0, rng.uniform(0, 1) / 60.0])
        my_predbat.iboost_min_soc = rng.choice([0, rng.randint(0, 100)])
        my_predbat.iboost_rate_threshold = rng.choice([9999, round(rng.uniform(0, 40), 2)])
        my_predbat.iboost_rate_threshold_export = rng.choice([9999, round(rng.uniform(0, 30), 2)])
        my_predbat.iboost_gas_scale = round(rng.uniform(0.5, 1.5), 2)
        my_predbat.iboost_today = round(rng.uniform(0, 5), 2)
        my_predbat.rate_gas = {minute: round(rng.uniform(2, 15), 2) for minute in range(0, my_predbat.forecast_minutes + my_predbat.minutes_now)} if rng.random() < 0.7 else {}
        if rng.random() < 0.5:
            plan = []
            for _ in range(rng.randint(1, 3)):
                start = my_predbat.minutes_now + rng.randrange(0, my_predbat.forecast_minutes - 60, 30)
                length = rng.randrange(30, 3 * 60, 30)
                plan.append({"start": start, "end": start + length, "kwh": round(rng.uniform(1, 10), 2)})
            my_predbat.iboost_plan = plan
        else:
            my_predbat.iboost_plan = []


def make_step_data(my_predbat, rng=None, pv_kw=0.0, load_kw=0.0):
    """Build pv/load step dictionaries, random per slot when an rng is given"""
    pv_step = {}
    load_step = {}
    pv10_step = {}
    load10_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        if rng:
            pv = round(rng.uniform(0, 0.4), 3) if rng.random() < 0.7 else 0.0
            load = round(rng.uniform(0, 0.5), 3)
            pv_step[minute] = pv
            load_step[minute] = load
            pv10_step[minute] = round(pv * rng.uniform(0.3, 1.0), 3)
            load10_step[minute] = round(load * rng.uniform(1.0, 1.2), 3)
        else:
            pv_step[minute] = pv_kw / 12.0
            load_step[minute] = load_kw / 12.0
            pv10_step[minute] = pv_kw / 24.0
            load10_step[minute] = load_kw / 12.0
    return pv_step, pv10_step, load_step, load10_step


def compare_results(name, python_result, kernel_result):
    """Compare the Python engine and kernel result tuples, returns True on failure"""
    failed = False
    for index, result_name in enumerate(RESULT_NAMES):
        python_value = python_result[index]
        kernel_value = kernel_result[index]
        if abs(python_value - kernel_value) > PARITY_TOLERANCE:
            print("ERROR: Scenario {} mismatch on {}: python {} kernel {}".format(name, result_name, python_value, kernel_value))
            failed = True

    python_soc = python_result[11]
    kernel_soc = kernel_result[11]
    if sorted(python_soc.keys()) != sorted(kernel_soc.keys()):
        print("ERROR: Scenario {} predict_soc keys mismatch: python {} kernel {} entries".format(name, len(python_soc), len(kernel_soc)))
        failed = True
    else:
        for minute in python_soc:
            if abs(python_soc[minute] - kernel_soc[minute]) > PARITY_TOLERANCE:
                print("ERROR: Scenario {} predict_soc mismatch at minute {}: python {} kernel {}".format(name, minute, python_soc[minute], kernel_soc[minute]))
                failed = True
                break

    for index, item_name in [(12, "car_charging_soc_next"), (13, "iboost_next"), (14, "iboost_running"), (15, "iboost_running_solar"), (16, "iboost_running_full")]:
        if python_result[index] != kernel_result[index]:
            print("ERROR: Scenario {} mismatch on {}: python {} kernel {}".format(name, item_name, python_result[index], kernel_result[index]))
            failed = True
    return failed


def dual_run(name, my_predbat, pv_step, pv10_step, load_step, load10_step, charge_limit, charge_window, export_window, export_limits, pv10, end_record):
    """Run one scenario through both engines and compare, returns True on failure"""
    # Python engine first (kernel disabled so run_prediction cannot dispatch)
    my_predbat.prediction_kernel_enable = False
    prediction = Prediction(my_predbat, pv_step, pv10_step, load_step, load10_step)
    python_result = prediction.run_prediction(charge_limit, charge_window, export_window, export_limits, pv10, end_record, save=None, cache=False)

    # Kernel run on the identical Prediction state
    prediction.kernel_handle = create_kernel_context(prediction)
    if not prediction.kernel_handle:
        print("ERROR: Scenario {} kernel context creation failed".format(name))
        return True
    kernel_result = run_prediction_kernel(prediction, charge_limit, charge_window, export_window, export_limits, pv10, end_record, 5, False)
    if kernel_result is None:
        print("ERROR: Scenario {} kernel run failed".format(name))
        return True

    failed = compare_results(name, python_result, kernel_result)

    # Also check the run_prediction dispatch glue path picks the kernel and agrees
    prediction.prediction_kernel_enable = True
    dispatch_result = prediction.run_prediction(charge_limit, charge_window, export_window, export_limits, pv10, end_record, save=None, cache=False)
    failed |= compare_results(name + "_dispatch", kernel_result, dispatch_result)
    return failed


def run_edge_case_tests(my_predbat):
    """Deterministic scenarios pinning each kernel branch, returns True on failure"""
    failed = False
    minutes_now = my_predbat.minutes_now
    forecast_minutes = my_predbat.forecast_minutes
    full_window = [{"start": minutes_now, "end": minutes_now + forecast_minutes, "average": 10}]
    half_window = [{"start": minutes_now, "end": minutes_now + forecast_minutes // 2, "average": 10}]

    cases = [
        # name, config overrides, charge_limit, charge_window, export_window, export_limits, pv_kw, load_kw, end_record
        ("idle", {}, [], [], [], [], 0, 0, forecast_minutes),
        ("load_only", {}, [], [], [], [], 0, 1.0, forecast_minutes),
        ("pv_only", {}, [], [], [], [], 2.0, 0, forecast_minutes),
        ("pv_load_battery", {"soc_kw": 50.0}, [], [], [], [], 2.0, 1.0, forecast_minutes),
        ("charge_full", {}, [100.0], full_window, [], [], 0, 0.5, forecast_minutes),
        ("charge_freeze", {"soc_kw": 50.0, "reserve": 4.0, "set_charge_freeze": True}, [4.0], full_window, [], [], 1.0, 0.5, forecast_minutes),
        ("charge_hybrid_pv", {"inverter_hybrid": True, "battery_rate_max_charge_dc": 2 / 60.0}, [100.0], full_window, [], [], 3.0, 0.5, forecast_minutes),
        ("export_full", {"soc_kw": 100.0}, [], [], half_window, [0.0], 0, 0.5, forecast_minutes),
        ("export_freeze", {"soc_kw": 100.0, "set_export_freeze": True}, [], [], half_window, [99.0], 1.0, 0.5, forecast_minutes),
        ("export_low_power", {"soc_kw": 100.0, "set_export_low_power": True}, [], [], half_window, [49.5], 0, 0.5, forecast_minutes),
        ("export_limited", {"soc_kw": 100.0, "export_limit": 0.5 / 60.0, "battery_rate_max_export": 2 / 60.0}, [], [], half_window, [0.0], 2.0, 0.2, forecast_minutes),
        ("export_no_charge_during", {"soc_kw": 100.0, "inverter_can_charge_during_export": False, "export_limit": 0.5 / 60.0}, [], [], half_window, [0.0], 3.0, 0.2, forecast_minutes),
        ("intersecting_windows", {"soc_kw": 50.0}, [100.0], full_window, half_window, [0.0], 1.0, 0.5, forecast_minutes),
        ("pv_ac_limit_clip", {"pv_ac_limit": 1 / 60.0}, [], [], [], [], 3.0, 0.2, forecast_minutes),
        ("small_inverter", {"soc_kw": 50.0, "inverter_limit": 0.5 / 60.0}, [], [], [], [], 2.0, 2.0, forecast_minutes),
        ("keep_metric", {"best_soc_keep": 10.0, "best_soc_keep_weight": 0.5, "soc_kw": 5.0}, [], [], [], [], 0, 1.0, forecast_minutes),
        ("end_record_zero", {"soc_kw": 20.0}, [], [], [], [], 1.0, 1.0, 0),
        ("end_record_half", {"soc_kw": 20.0}, [], [], [], [], 1.0, 1.0, forecast_minutes // 2),
        ("misaligned_window", {"soc_kw": 50.0}, [100.0], [{"start": minutes_now + 3, "end": minutes_now + 63, "average": 10}], [], [], 0, 0.5, forecast_minutes),
        ("no_discharge_during_charge", {"soc_kw": 50.0, "set_discharge_during_charge": False}, [50.0], half_window, [], [], 0, 1.0, forecast_minutes),
        ("cold_battery", {"battery_temperature": 2, "soc_kw": 20.0}, [100.0], half_window, [], [], 1.0, 1.0, forecast_minutes),
        ("carbon", {"carbon_enable": True, "carbon_intensity": {minute: 100 + (minute % 60) for minute in range(0, forecast_minutes, 5)}, "carbon_today_sofar": 500.0, "soc_kw": 20.0}, [], [], [], [], 1.0, 1.0, forecast_minutes),
        (
            "car_charging",
            {
                "num_cars": 1,
                "car_charging_slots": [[{"start": minutes_now + 60, "end": minutes_now + 240, "kwh": 21.0, "average": 30, "octopus": True}], [], [], []],
                "car_charging_soc": [10, 0, 0, 0],
                "car_charging_limit": [50, 100, 100, 100],
                "car_charging_loss": 0.9,
                "soc_kw": 30.0,
            },
            [],
            [],
            [],
            [],
            0,
            0.5,
            forecast_minutes,
        ),
        (
            "car_not_reported",
            {"num_cars": 1, "car_energy_reported_load": False, "car_charging_slots": [[{"start": minutes_now, "end": minutes_now + 300, "kwh": 15.0, "average": 0, "octopus": False}], [], [], []], "car_charging_soc": [0, 0, 0, 0], "soc_kw": 50.0},
            [],
            [],
            [],
            [],
            2.0,
            0.5,
            forecast_minutes,
        ),
        (
            "car_no_charge_from_battery",
            {"num_cars": 1, "car_charging_from_battery": False, "car_charging_slots": [[{"start": minutes_now, "end": minutes_now + 300, "kwh": 15.0, "average": 0, "octopus": False}], [], [], []], "car_charging_soc": [0, 0, 0, 0], "soc_kw": 50.0},
            [],
            [],
            [],
            [],
            0,
            0.5,
            forecast_minutes,
        ),
        (
            "iboost_charging",
            {
                "iboost_enable": True,
                "iboost_charging": True,
                "iboost_max_energy": 5.0,
                "iboost_max_power": 3.0 / 60.0,
                "iboost_min_power": 0.0,
                "iboost_min_soc": 0,
                "iboost_rate_threshold": 9999,
                "iboost_rate_threshold_export": 9999,
                "iboost_today": 0.5,
                "iboost_plan": [],
            },
            [100.0],
            half_window,
            [],
            [],
            0,
            0.5,
            forecast_minutes,
        ),
        (
            "iboost_solar",
            {
                "iboost_enable": True,
                "iboost_solar": True,
                "iboost_solar_excess": False,
                "iboost_max_energy": 8.0,
                "iboost_max_power": 2.0 / 60.0,
                "iboost_min_power": 0.1 / 60.0,
                "iboost_min_soc": 10,
                "iboost_rate_threshold": 9999,
                "iboost_rate_threshold_export": 9999,
                "iboost_today": 0.0,
                "iboost_plan": [],
                "soc_kw": 50.0,
            },
            [],
            [],
            [],
            [],
            3.0,
            0.5,
            forecast_minutes,
        ),
        (
            "iboost_solar_excess",
            {
                "iboost_enable": True,
                "iboost_solar": True,
                "iboost_solar_excess": True,
                "iboost_max_energy": 8.0,
                "iboost_max_power": 2.0 / 60.0,
                "iboost_min_power": 0.1 / 60.0,
                "iboost_min_soc": 0,
                "iboost_rate_threshold": 9999,
                "iboost_rate_threshold_export": 9999,
                "iboost_today": 0.0,
                "iboost_plan": [],
                "soc_kw": 100.0,
            },
            [],
            [],
            [],
            [],
            3.0,
            0.2,
            forecast_minutes,
        ),
        (
            "iboost_gas",
            {
                "iboost_enable": True,
                "iboost_charging": True,
                "iboost_gas": True,
                "iboost_gas_scale": 1.1,
                "rate_gas": {minute: 7.0 for minute in range(forecast_minutes + minutes_now)},
                "iboost_max_energy": 5.0,
                "iboost_max_power": 3.0 / 60.0,
                "iboost_min_power": 0.0,
                "iboost_min_soc": 0,
                "iboost_rate_threshold": 9999,
                "iboost_rate_threshold_export": 9999,
                "iboost_today": 0.0,
                "iboost_plan": [],
            },
            [100.0],
            half_window,
            [],
            [],
            0,
            0.5,
            forecast_minutes,
        ),
        (
            "iboost_plan",
            {
                "iboost_enable": True,
                "iboost_plan": [{"start": minutes_now + 60, "end": minutes_now + 180, "kwh": 4.0}],
                "iboost_max_energy": 5.0,
                "iboost_max_power": 3.0 / 60.0,
                "iboost_min_power": 0.0,
                "iboost_min_soc": 0,
                "iboost_rate_threshold": 9999,
                "iboost_rate_threshold_export": 9999,
                "iboost_today": 0.0,
            },
            [],
            [],
            [],
            [],
            0,
            0.5,
            forecast_minutes,
        ),
    ]

    for name, overrides, charge_limit, charge_window, export_window, export_limits, pv_kw, load_kw, end_record in cases:
        reset_inverter(my_predbat)
        reset_rates(my_predbat, 10.0, 5.0)
        my_predbat.battery_rate_max_export = my_predbat.battery_rate_max_discharge
        for key, value in overrides.items():
            setattr(my_predbat, key, value)
        pv_step, pv10_step, load_step, load10_step = make_step_data(my_predbat, pv_kw=pv_kw, load_kw=load_kw)
        for pv10 in [False, True]:
            failed |= dual_run(
                "{}_pv10_{}".format(name, pv10), my_predbat, pv_step, pv10_step, load_step, load10_step, charge_limit[:], [dict(window) for window in charge_window], [dict(window) for window in export_window], export_limits[:], pv10, end_record
            )
    return failed


def run_random_sweep_tests(my_predbat, count=150):
    """Seeded random configuration sweep comparing both engines, returns True on failure"""
    failed = False
    for seed in range(count):
        rng = random.Random(seed)
        reset_inverter(my_predbat)
        reset_rates(my_predbat, 10.0, 5.0)
        my_predbat.battery_rate_max_export = my_predbat.battery_rate_max_discharge
        apply_random_scenario(my_predbat, rng)
        pv_step, pv10_step, load_step, load10_step = make_step_data(my_predbat, rng=rng)

        charge_window = make_windows(rng, my_predbat.minutes_now, my_predbat.forecast_minutes, rng.randint(0, 3), align=rng.choice([5, 5, 30, 3]))
        charge_limit = [rng.choice([0.0, my_predbat.reserve, my_predbat.soc_max, round(rng.uniform(0, my_predbat.soc_max), 2)]) for _ in charge_window]
        export_window = make_windows(rng, my_predbat.minutes_now, my_predbat.forecast_minutes, rng.randint(0, 3), align=rng.choice([5, 5, 30]))
        export_limits = [rng.choice([100.0, 99.0, 0.0, round(rng.uniform(0, 100), 1)]) for _ in export_window]
        end_record = rng.choice([my_predbat.forecast_minutes, my_predbat.forecast_minutes - 30, rng.randrange(0, my_predbat.forecast_minutes, 5)])
        pv10 = rng.choice([False, True])

        failed |= dual_run("random_{}".format(seed), my_predbat, pv_step, pv10_step, load_step, load10_step, charge_limit, charge_window, export_window, export_limits, pv10, end_record)
        if failed:
            print("Random sweep failed at seed {}".format(seed))
            break
    return failed


def kernel_available():
    """Ensure the kernel library is built and loaded, returns (available, required_failure)"""
    if not ensure_kernel_built():
        if os.environ.get("PREDBAT_KERNEL_REQUIRED"):
            print("ERROR: Prediction kernel is required (PREDBAT_KERNEL_REQUIRED) but could not be built/loaded")
            return False, True
        print("WARNING: Prediction kernel not available ({}) - kernel tests SKIPPED".format(prediction_kernel.KERNEL_STATUS))
        return False, False
    return True, False


def run_model_kernel_tests(my_predbat):
    """Run the standard model test suite (run_model_tests) with the C++ prediction kernel enabled.

    Kernel-supported scenarios dispatch their predictions to the C++ kernel and the
    normal model asserts validate the results; unsupported scenarios (cars, iBoost,
    carbon, low-power charge) fall back to the Python engine as in production.
    Returns True on failure.
    """
    available, required_failure = kernel_available()
    if not available:
        return required_failure

    try:
        return run_model_tests(my_predbat, prediction_kernel=True)
    finally:
        my_predbat.prediction_kernel_enable = False


def run_kernel_parity_tests(my_predbat):
    """Compare the C++ prediction kernel against the Python engine, returns True on failure"""
    print("**** Running kernel parity tests ****")

    available, required_failure = kernel_available()
    if not available:
        return required_failure

    state = snapshot_scenario_state(my_predbat)
    try:
        failed = run_edge_case_tests(my_predbat)
        if not failed:
            failed |= run_random_sweep_tests(my_predbat)
    finally:
        restore_scenario_state(my_predbat, state)

    if failed:
        print("**** Kernel parity tests FAILED ****")
    else:
        print("**** Kernel parity tests passed ****")
    return failed
