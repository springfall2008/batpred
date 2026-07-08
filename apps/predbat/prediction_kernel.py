# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Loader and marshalling layer for the C++ prediction kernel.

The kernel (prediction_kernel.cpp) is a fast mirror of the hot loop in
Prediction.run_prediction(). It is loaded as a plain C ABI shared library via
ctypes; if the library is missing, stale or fails to load, everything falls
back to the Python engine transparently.

PARITY RULE: any behavioural change to the hot loop in prediction.py must be
mirrored in prediction_kernel.cpp and KERNEL_PARITY_REVISION below and
PK_PARITY_REVISION in the .cpp must both be bumped so a stale binary is
rejected at load time rather than producing divergent results.
"""

import ctypes
import os
import platform
import sys
import weakref

from const import PREDICT_STEP
from utils import remove_intersecting_windows, get_curve_value, find_battery_temperature_cap, in_car_slot, in_iboost_slot

# Expected ABI/parity revisions of the shared library (see prediction_kernel.cpp)
KERNEL_ABI_VERSION = 2
KERNEL_PARITY_REVISION = 2

# Maximum number of cars supported by the kernel (PK_MAX_CARS in prediction_kernel.cpp)
KERNEL_MAX_CARS = 4

# Module-level loader state, inherited by forked worker processes
KERNEL_LIB = None
KERNEL_LOAD_TRIED = False
KERNEL_STATUS = "not loaded"


class PkContext(ctypes.Structure):
    """ctypes mirror of the PkContext struct in prediction_kernel.cpp (field order must match exactly)"""

    _fields_ = [
        ("rate_import", ctypes.POINTER(ctypes.c_double)),
        ("rate_export", ctypes.POINTER(ctypes.c_double)),
        ("alert_keep", ctypes.POINTER(ctypes.c_double)),
        ("pv", ctypes.POINTER(ctypes.c_double)),
        ("load", ctypes.POINTER(ctypes.c_double)),
        ("pv10", ctypes.POINTER(ctypes.c_double)),
        ("load10", ctypes.POINTER(ctypes.c_double)),
        ("temp_charge_cap", ctypes.POINTER(ctypes.c_double)),
        ("temp_discharge_cap", ctypes.POINTER(ctypes.c_double)),
        ("io_flag", ctypes.POINTER(ctypes.c_int32)),
        ("charge_curve", ctypes.POINTER(ctypes.c_double)),
        ("discharge_curve", ctypes.POINTER(ctypes.c_double)),
        ("carbon", ctypes.POINTER(ctypes.c_double)),
        ("gas_rate", ctypes.POINTER(ctypes.c_double)),
        ("iboost_plan_load", ctypes.POINTER(ctypes.c_double)),
        ("car_load_flat", ctypes.POINTER(ctypes.c_double)),
        ("car_rate_flat", ctypes.POINTER(ctypes.c_double)),
        ("soc_kw", ctypes.c_double),
        ("soc_max", ctypes.c_double),
        ("reserve", ctypes.c_double),
        ("best_soc_min", ctypes.c_double),
        ("best_soc_keep", ctypes.c_double),
        ("best_soc_keep_weight", ctypes.c_double),
        ("battery_loss", ctypes.c_double),
        ("battery_loss_discharge", ctypes.c_double),
        ("inverter_loss", ctypes.c_double),
        ("inverter_limit", ctypes.c_double),
        ("export_limit", ctypes.c_double),
        ("pv_ac_limit", ctypes.c_double),
        ("battery_rate_min", ctypes.c_double),
        ("battery_rate_max_charge", ctypes.c_double),
        ("battery_rate_max_charge_dc", ctypes.c_double),
        ("battery_rate_max_discharge", ctypes.c_double),
        ("battery_rate_max_export", ctypes.c_double),
        ("battery_rate_max_scaling", ctypes.c_double),
        ("battery_rate_max_scaling10", ctypes.c_double),
        ("battery_rate_max_scaling_discharge", ctypes.c_double),
        ("charge_rate_now", ctypes.c_double),
        ("discharge_rate_now", ctypes.c_double),
        ("rate_max", ctypes.c_double),
        ("cost_today_sofar", ctypes.c_double),
        ("carbon_today_sofar", ctypes.c_double),
        ("export_today_now", ctypes.c_double),
        ("iboost_today", ctypes.c_double),
        ("car_charging_loss", ctypes.c_double),
        ("car_charging_limit", ctypes.c_double * 4),
        ("car_charging_soc", ctypes.c_double * 4),
        ("iboost_max_energy", ctypes.c_double),
        ("iboost_max_power", ctypes.c_double),
        ("iboost_min_power", ctypes.c_double),
        ("iboost_min_soc", ctypes.c_double),
        ("iboost_rate_threshold", ctypes.c_double),
        ("iboost_rate_threshold_export", ctypes.c_double),
        ("n_steps", ctypes.c_int32),
        ("minutes_now", ctypes.c_int32),
        ("forecast_minutes", ctypes.c_int32),
        ("inverter_hybrid", ctypes.c_int32),
        ("set_charge_freeze", ctypes.c_int32),
        ("set_reserve_enable", ctypes.c_int32),
        ("set_export_freeze", ctypes.c_int32),
        ("set_export_freeze_only", ctypes.c_int32),
        ("set_charge_window", ctypes.c_int32),
        ("set_export_window", ctypes.c_int32),
        ("set_discharge_during_charge", ctypes.c_int32),
        ("set_export_low_power", ctypes.c_int32),
        ("calculate_export_on_pv", ctypes.c_int32),
        ("inverter_can_charge_during_export", ctypes.c_int32),
        ("num_cars", ctypes.c_int32),
        ("car_energy_reported_load", ctypes.c_int32),
        ("car_charging_from_battery", ctypes.c_int32),
        ("carbon_enable", ctypes.c_int32),
        ("iboost_enable", ctypes.c_int32),
        ("iboost_solar", ctypes.c_int32),
        ("iboost_solar_excess", ctypes.c_int32),
        ("iboost_gas", ctypes.c_int32),
        ("iboost_gas_export", ctypes.c_int32),
        ("iboost_charging", ctypes.c_int32),
        ("iboost_prevent_discharge", ctypes.c_int32),
        ("iboost_on_export", ctypes.c_int32),
        ("has_rate_gas", ctypes.c_int32),
        ("has_iboost_plan", ctypes.c_int32),
    ]


class PkScenario(ctypes.Structure):
    """ctypes mirror of the PkScenario struct in prediction_kernel.cpp (field order must match exactly)"""

    _fields_ = [
        ("charge_limit", ctypes.POINTER(ctypes.c_double)),
        ("charge_start", ctypes.POINTER(ctypes.c_int32)),
        ("charge_end", ctypes.POINTER(ctypes.c_int32)),
        ("export_limits", ctypes.POINTER(ctypes.c_double)),
        ("export_start", ctypes.POINTER(ctypes.c_int32)),
        ("export_end", ctypes.POINTER(ctypes.c_int32)),
        ("soc_out", ctypes.POINTER(ctypes.c_double)),
        ("n_charge", ctypes.c_int32),
        ("n_export", ctypes.c_int32),
        ("pv10", ctypes.c_int32),
        ("end_record", ctypes.c_int32),
        ("step", ctypes.c_int32),
    ]


class PkResult(ctypes.Structure):
    """ctypes mirror of the PkResult struct in prediction_kernel.cpp (field order must match exactly)"""

    _fields_ = [
        ("final_metric", ctypes.c_double),
        ("import_kwh_battery", ctypes.c_double),
        ("import_kwh_house", ctypes.c_double),
        ("export_kwh", ctypes.c_double),
        ("soc_min", ctypes.c_double),
        ("final_soc", ctypes.c_double),
        ("battery_cycle", ctypes.c_double),
        ("metric_keep", ctypes.c_double),
        ("final_iboost", ctypes.c_double),
        ("final_carbon_g", ctypes.c_double),
        ("car_soc_next", ctypes.c_double * 4),
        ("iboost_next", ctypes.c_double),
        ("soc_min_minute", ctypes.c_int32),
        ("car_soc_next_valid", ctypes.c_int32),
        ("iboost_running", ctypes.c_int32),
        ("iboost_running_solar", ctypes.c_int32),
        ("iboost_running_full", ctypes.c_int32),
    ]


def kernel_library_candidates():
    """Return the ordered list of shared library paths to try loading.

    Order: PREDBAT_KERNEL_SO override, then a local/Docker native build, then the
    cross-built per-architecture binary named by platform.machine(). Candidates
    that are missing or fail to load are skipped (Python engine fallback).
    """
    candidates = []
    env_path = os.environ.get("PREDBAT_KERNEL_SO")
    if env_path:
        candidates.append(env_path)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    # Local development/Docker build (named _lib so Python's importer never picks it up in place of this module)
    candidates.append(os.path.join(base_dir, "prediction_kernel_lib.so"))
    # Cross-built per-architecture binaries (see build_kernel_cross.sh, delivered by
    # self-update); Linux ones are glibc-based as the addon images are Ubuntu
    machine = platform.machine().lower()
    if machine and sys.platform == "linux":
        candidates.append(os.path.join(base_dir, "prediction_kernel_lib_{}.so".format(machine)))
    elif machine and sys.platform == "darwin":
        candidates.append(os.path.join(base_dir, "prediction_kernel_lib_darwin_{}.so".format(machine)))
    return candidates


def kernel_status_summary(pred):
    """Return (message, is_warning) describing whether the kernel is active for this Prediction.

    Intended for a per-plan-cycle status log line, e.g. self.log("Prediction kernel: " + message).
    is_warning is True only for the "enabled but not available" case, which is worth flagging
    since it means every prediction this cycle silently falls back to the slower Python engine.
    """
    if not getattr(pred, "prediction_kernel_enable", False):
        return "disabled", False
    if getattr(pred, "kernel_handle", 0):
        return "enabled and active ({})".format(KERNEL_STATUS), False
    return "enabled but NOT available ({}) - falling back to the Python engine".format(KERNEL_STATUS), True


def load_kernel(log=None):
    """Load and verify the kernel shared library, returns the ctypes library or None.

    The result is cached module-wide (including across forked workers); failures
    are logged once and result in a permanent fallback to the Python engine.
    """
    global KERNEL_LIB, KERNEL_LOAD_TRIED, KERNEL_STATUS

    if KERNEL_LOAD_TRIED:
        return KERNEL_LIB
    KERNEL_LOAD_TRIED = True

    for path in kernel_library_candidates():
        if not os.path.exists(path):
            continue
        try:
            lib = ctypes.CDLL(path)
            lib.pk_abi_version.restype = ctypes.c_int32
            lib.pk_abi_version.argtypes = []
            lib.pk_parity_revision.restype = ctypes.c_int32
            lib.pk_parity_revision.argtypes = []
            abi = lib.pk_abi_version()
            revision = lib.pk_parity_revision()
            if abi != KERNEL_ABI_VERSION or revision != KERNEL_PARITY_REVISION:
                KERNEL_STATUS = "stale binary {} (abi {} revision {}, expected abi {} revision {})".format(path, abi, revision, KERNEL_ABI_VERSION, KERNEL_PARITY_REVISION)
                if log:
                    log("Warn: Prediction kernel {} - using Python engine".format(KERNEL_STATUS))
                continue
            lib.pk_context_create.restype = ctypes.c_int64
            lib.pk_context_create.argtypes = [ctypes.POINTER(PkContext)]
            lib.pk_context_free.restype = None
            lib.pk_context_free.argtypes = [ctypes.c_int64]
            lib.pk_run.restype = ctypes.c_int32
            lib.pk_run.argtypes = [ctypes.c_int64, ctypes.POINTER(PkScenario), ctypes.POINTER(PkResult)]
            KERNEL_LIB = lib
            KERNEL_STATUS = "loaded from {}".format(path)
            if log:
                log("Prediction kernel {}".format(KERNEL_STATUS))
            return KERNEL_LIB
        except (OSError, AttributeError) as error:
            KERNEL_STATUS = "failed to load {} ({})".format(path, error)
            if log:
                log("Warn: Prediction kernel {} - using Python engine".format(KERNEL_STATUS))

    return KERNEL_LIB


def double_array(values):
    """Create a ctypes double array from a Python list"""
    return (ctypes.c_double * len(values))(*values)


def int32_array(values):
    """Create a ctypes int32 array from a Python list"""
    return (ctypes.c_int32 * len(values))(*values)


def kernel_context_free(handle):
    """Free a kernel context by handle (used as a weakref finaliser)"""
    if KERNEL_LIB and handle:
        KERNEL_LIB.pk_context_free(handle)


def create_kernel_context(pred):
    """Build the per-plan static context for a Prediction object and hand it to the kernel.

    Returns the kernel context handle (>0) or 0 when the kernel is unavailable or
    the context could not be built; 0 means the Python engine will be used.
    """
    lib = load_kernel(log=pred.log)
    if not lib:
        return 0

    try:
        forecast_minutes = pred.forecast_minutes
        minutes_now = pred.minutes_now
        num_cars = pred.num_cars
        if forecast_minutes <= 0 or (forecast_minutes % PREDICT_STEP) != 0 or num_cars > KERNEL_MAX_CARS:
            return 0
        n_steps = forecast_minutes // PREDICT_STEP

        rate_import = []
        rate_export = []
        alert_keep = []
        io_flag = []
        pv = []
        load = []
        pv10 = []
        load10 = []
        temp_charge_cap = []
        temp_discharge_cap = []
        carbon = []
        gas_rate = []
        iboost_plan_load = []
        car_load_flat = [0.0] * (num_cars * n_steps)
        car_rate_flat = [0.0] * (num_cars * n_steps)
        for k in range(n_steps):
            minute = k * PREDICT_STEP
            minute_absolute = minute + minutes_now
            rate_import.append(pred.rate_import.get(minute_absolute, 0))
            rate_export.append(pred.rate_export.get(minute_absolute, 0))
            alert_keep.append(pred.all_active_keep.get(minute_absolute, 0))
            io_flag.append(1 if pred.io_adjusted.get(minute_absolute, 0) else 0)
            pv.append(pred.pv_forecast_minute_step[minute])
            load.append(pred.load_minutes_step[minute])
            pv10.append(pred.pv_forecast_minute10_step[minute])
            load10.append(pred.load_minutes_step10[minute])
            # Pre-compute the temperature rate cap base (before the min against the max rate,
            # which the kernel applies per lookup) - mirrors utils.py find_battery_temperature_cap
            battery_temperature = pred.battery_temperature_prediction.get(minute, pred.battery_temperature)
            temp_charge_cap.append(find_battery_temperature_cap(battery_temperature, pred.battery_temperature_charge_curve, pred.soc_max, float("inf")))
            temp_discharge_cap.append(find_battery_temperature_cap(battery_temperature, pred.battery_temperature_discharge_curve, pred.soc_max, float("inf")))
            carbon.append(pred.carbon_intensity.get(minute, 0) if pred.carbon_intensity else 0)
            # Gas rate pre-scaled by iboost_gas_scale - mirrors prediction.py:719/725
            gas_rate.append((pred.rate_gas.get(minute_absolute, 99) * pred.iboost_gas_scale) if pred.rate_gas else 0)
            iboost_plan_load.append(in_iboost_slot(minute_absolute, pred.iboost_plan) if pred.iboost_plan else 0)
            if num_cars > 0:
                car_load, car_rate_slot = in_car_slot(minute_absolute, num_cars, pred.car_charging_slots)
                for car_n in range(num_cars):
                    car_load_flat[car_n * n_steps + k] = car_load[car_n]
                    car_rate_flat[car_n * n_steps + k] = car_rate_slot[car_n]

        # Raw power curve multipliers by SoC percent - mirrors utils.py get_curve_value
        charge_curve = [get_curve_value(pred.battery_charge_power_curve, percent, 1.0) for percent in range(101)]
        discharge_curve = [get_curve_value(pred.battery_discharge_power_curve, percent, 1.0) for percent in range(101)]

        ctx = PkContext()
        ctx.rate_import = double_array(rate_import)
        ctx.rate_export = double_array(rate_export)
        ctx.alert_keep = double_array(alert_keep)
        ctx.pv = double_array(pv)
        ctx.load = double_array(load)
        ctx.pv10 = double_array(pv10)
        ctx.load10 = double_array(load10)
        ctx.temp_charge_cap = double_array(temp_charge_cap)
        ctx.temp_discharge_cap = double_array(temp_discharge_cap)
        ctx.io_flag = int32_array(io_flag)
        ctx.charge_curve = double_array(charge_curve)
        ctx.discharge_curve = double_array(discharge_curve)
        ctx.carbon = double_array(carbon)
        ctx.gas_rate = double_array(gas_rate)
        ctx.iboost_plan_load = double_array(iboost_plan_load)
        ctx.car_load_flat = double_array(car_load_flat)
        ctx.car_rate_flat = double_array(car_rate_flat)

        ctx.soc_kw = pred.soc_kw
        ctx.soc_max = pred.soc_max
        ctx.reserve = pred.reserve
        ctx.best_soc_min = pred.best_soc_min
        ctx.best_soc_keep = pred.best_soc_keep
        ctx.best_soc_keep_weight = pred.best_soc_keep_weight
        ctx.battery_loss = pred.battery_loss
        ctx.battery_loss_discharge = pred.battery_loss_discharge
        ctx.inverter_loss = pred.inverter_loss
        ctx.inverter_limit = pred.inverter_limit
        ctx.export_limit = pred.export_limit
        ctx.pv_ac_limit = pred.pv_ac_limit
        ctx.battery_rate_min = pred.battery_rate_min
        ctx.battery_rate_max_charge = pred.battery_rate_max_charge
        ctx.battery_rate_max_charge_dc = pred.battery_rate_max_charge_dc
        ctx.battery_rate_max_discharge = pred.battery_rate_max_discharge
        ctx.battery_rate_max_export = pred.battery_rate_max_export
        ctx.battery_rate_max_scaling = pred.battery_rate_max_scaling
        ctx.battery_rate_max_scaling10 = pred.battery_rate_max_scaling * pred.charge_scaling10
        ctx.battery_rate_max_scaling_discharge = pred.battery_rate_max_scaling_discharge
        ctx.charge_rate_now = pred.charge_rate_now
        ctx.discharge_rate_now = pred.discharge_rate_now
        ctx.rate_max = pred.rate_max
        ctx.cost_today_sofar = pred.cost_today_sofar
        ctx.carbon_today_sofar = pred.carbon_today_sofar
        ctx.export_today_now = pred.export_today_now
        ctx.iboost_today = pred.iboost_today
        ctx.car_charging_loss = pred.car_charging_loss
        for car_n in range(num_cars):
            ctx.car_charging_limit[car_n] = pred.car_charging_limit[car_n]
            ctx.car_charging_soc[car_n] = pred.car_charging_soc[car_n]
        ctx.iboost_max_energy = pred.iboost_max_energy
        ctx.iboost_max_power = pred.iboost_max_power
        ctx.iboost_min_power = pred.iboost_min_power
        ctx.iboost_min_soc = pred.iboost_min_soc
        ctx.iboost_rate_threshold = pred.iboost_rate_threshold
        ctx.iboost_rate_threshold_export = pred.iboost_rate_threshold_export

        ctx.n_steps = n_steps
        ctx.minutes_now = minutes_now
        ctx.forecast_minutes = forecast_minutes
        ctx.inverter_hybrid = 1 if pred.inverter_hybrid else 0
        ctx.set_charge_freeze = 1 if pred.set_charge_freeze else 0
        ctx.set_reserve_enable = 1 if pred.set_reserve_enable else 0
        ctx.set_export_freeze = 1 if pred.set_export_freeze else 0
        ctx.set_export_freeze_only = 1 if pred.set_export_freeze_only else 0
        ctx.set_charge_window = 1 if pred.set_charge_window else 0
        ctx.set_export_window = 1 if pred.set_export_window else 0
        ctx.set_discharge_during_charge = 1 if pred.set_discharge_during_charge else 0
        ctx.set_export_low_power = 1 if pred.set_export_low_power else 0
        ctx.calculate_export_on_pv = 1 if pred.calculate_export_on_pv else 0
        ctx.inverter_can_charge_during_export = 1 if pred.inverter_can_charge_during_export else 0
        ctx.num_cars = num_cars
        ctx.car_energy_reported_load = 1 if pred.car_energy_reported_load else 0
        ctx.car_charging_from_battery = 1 if pred.car_charging_from_battery else 0
        ctx.carbon_enable = 1 if pred.carbon_enable else 0
        ctx.iboost_enable = 1 if pred.iboost_enable else 0
        ctx.iboost_solar = 1 if pred.iboost_solar else 0
        ctx.iboost_solar_excess = 1 if pred.iboost_solar_excess else 0
        ctx.iboost_gas = 1 if pred.iboost_gas else 0
        ctx.iboost_gas_export = 1 if pred.iboost_gas_export else 0
        ctx.iboost_charging = 1 if pred.iboost_charging else 0
        ctx.iboost_prevent_discharge = 1 if pred.iboost_prevent_discharge else 0
        ctx.iboost_on_export = 1 if pred.iboost_on_export else 0
        ctx.has_rate_gas = 1 if pred.rate_gas else 0
        ctx.has_iboost_plan = 1 if pred.iboost_plan else 0

        handle = lib.pk_context_create(ctypes.byref(ctx))
        if handle:
            weakref.finalize(pred, kernel_context_free, handle)
        return handle
    except (KeyError, TypeError, AttributeError) as error:
        if pred.log:
            pred.log("Warn: Prediction kernel context build failed ({}) - using Python engine".format(error))
        return 0


def kernel_supported(pred, save, step):
    """Check whether the kernel supports this prediction run (unsupported means Python fallback, never approximation)

    Any requested step is supported: the kernel always simulates at PREDICT_STEP (5 minute)
    granularity internally, which is strictly finer than the coarse "fast mode" step (e.g. 30)
    the Python engine falls back to for speed - so kernel runs are both faster and more accurate
    than a coarse-step Python run, never an approximation of what was asked for.
    """
    return not save and not pred.debug_enable and getattr(pred, "kernel_handle", 0) != 0


def run_prediction_kernel(pred, charge_limit, charge_window, export_window, export_limits, pv10, end_record, step, cache):
    """Run one prediction scenario through the C++ kernel.

    The caller's requested step is intentionally ignored - the kernel always simulates at
    PREDICT_STEP (5 minutes), see kernel_supported() - so a coarse "fast mode" step never
    reaches the kernel or the shared library ABI.

    Returns the same 17-tuple as Prediction.run_prediction() or None when the
    kernel could not run the scenario (caller falls back to the Python engine).
    """
    lib = KERNEL_LIB
    if not lib:
        return None

    # Remove intersecting windows, mirroring the Python engine - prediction.py:492-493
    charge_limit, charge_window = remove_intersecting_windows(charge_limit, charge_window, export_limits, export_window)

    n_steps = pred.forecast_minutes // PREDICT_STEP
    scenario = PkScenario()
    scenario.charge_limit = double_array([float(limit) for limit in charge_limit])
    scenario.charge_start = int32_array([window["start"] for window in charge_window])
    scenario.charge_end = int32_array([window["end"] for window in charge_window])
    scenario.export_limits = double_array([float(limit) for limit in export_limits])
    scenario.export_start = int32_array([window["start"] for window in export_window])
    scenario.export_end = int32_array([window["end"] for window in export_window])
    soc_out = (ctypes.c_double * n_steps)()
    scenario.soc_out = soc_out
    scenario.n_charge = len(charge_window)
    scenario.n_export = len(export_window)
    scenario.pv10 = 1 if pv10 else 0
    scenario.end_record = end_record
    scenario.step = PREDICT_STEP  # the caller's step is ignored - see run_prediction_kernel docstring

    result = PkResult()
    return_code = lib.pk_run(pred.kernel_handle, ctypes.byref(scenario), ctypes.byref(result))
    if return_code != 0:
        return None

    # Reset the per-run state attributes exactly as the Python engine does - prediction.py:414-422
    # (non-save runs never populate these, so they remain empty/False)
    pred.predict_soc_best = {}
    pred.predict_metric_best = {}
    pred.predict_iboost_best = {}
    pred.predict_carbon_best = {}
    pred.predict_clipped_best = {}
    pred.iboost_running = False
    pred.iboost_running_solar = False
    pred.iboost_running_full = False

    # Assemble the same return value as the Python engine - prediction.py:626-628, 1266-1284
    predict_soc = {}
    if not cache:
        for k in range(n_steps):
            predict_soc[k * PREDICT_STEP] = soc_out[k]

    car_charging_soc_next = pred.car_charging_soc_next[:]
    if result.car_soc_next_valid:
        for car_n in range(pred.num_cars):
            car_charging_soc_next[car_n] = result.car_soc_next[car_n]

    iboost_next = result.iboost_next if pred.iboost_enable else pred.iboost_next

    return (
        round(result.final_metric, 4),
        round(result.import_kwh_battery, 4),
        round(result.import_kwh_house, 4),
        round(result.export_kwh, 4),
        round(result.soc_min, 4),
        round(result.final_soc, 4),
        result.soc_min_minute,
        round(result.battery_cycle, 4),
        round(result.metric_keep, 4),
        round(result.final_iboost, 4),
        round(result.final_carbon_g, 4),
        predict_soc,
        car_charging_soc_next,
        iboost_next,
        bool(result.iboost_running),
        bool(result.iboost_running_solar),
        bool(result.iboost_running_full),
    )
