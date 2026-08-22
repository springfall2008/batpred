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

import array
import ctypes
import os
import platform
import sys
import weakref

from const import PREDICT_STEP, PREDBAT_MAX_CARS
from utils import get_curve_value, find_battery_temperature_cap, in_car_slot, in_iboost_slot

# Expected ABI/parity revisions of the shared library (see prediction_kernel.cpp)
KERNEL_ABI_VERSION = 5
KERNEL_PARITY_REVISION = 9

# Maximum number of cars supported by the kernel (PK_MAX_CARS in prediction_kernel.cpp)
KERNEL_MAX_CARS = PREDBAT_MAX_CARS

# Module-level loader state - the library is loaded once per process and shared by every Prediction
KERNEL_LIB = None
KERNEL_LOAD_TRIED = False
KERNEL_STATUS = "not loaded"
# True when the loaded binary exposes pk_run_batch. Probed rather than required so a kernel built
# before the batch entry point existed still runs every scenario through the single-call path.
KERNEL_HAS_BATCH = False


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
        ("pv90", ctypes.POINTER(ctypes.c_double)),
        ("load90", ctypes.POINTER(ctypes.c_double)),
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
        ("inverter_freeze_export_discharge_rate", ctypes.c_double),
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
        ("car_charging_limit", ctypes.c_double * KERNEL_MAX_CARS),
        ("car_charging_soc", ctypes.c_double * KERNEL_MAX_CARS),
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
        ("pv_scenario", ctypes.c_int32),
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
        ("car_soc_next", ctypes.c_double * KERNEL_MAX_CARS),
        ("iboost_next", ctypes.c_double),
        ("soc_min_minute", ctypes.c_int32),
        ("car_soc_next_valid", ctypes.c_int32),
        ("iboost_running", ctypes.c_int32),
        ("iboost_running_solar", ctypes.c_int32),
        ("iboost_running_full", ctypes.c_int32),
    ]


class PkBatchJob(ctypes.Structure):
    """ctypes mirror of the PkBatchJob struct in prediction_kernel.cpp (field order must match exactly)"""

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
        ("pv_scenario", ctypes.c_int32),
        ("end_record", ctypes.c_int32),
        ("step", ctypes.c_int32),
        ("soc_range_start_step", ctypes.c_int32),
        ("soc_range_end_step", ctypes.c_int32),
    ]


class PkBatchResult(ctypes.Structure):
    """ctypes mirror of the PkBatchResult struct in prediction_kernel.cpp (field order must match exactly)"""

    _fields_ = [
        ("result", PkResult),
        ("soc_range_min", ctypes.c_double),
        ("soc_range_max", ctypes.c_double),
        ("status", ctypes.c_int32),
        ("pad", ctypes.c_int32),
    ]


class BatchJob:
    """One queued prediction: its trial inputs, how the result should be shaped, and the result itself.

    run_prediction_kernel_batch reads only the input fields (charge_limit, charge_window,
    export_window, export_limits, pv_scenario, end_record) and the SoC range steps. step, sim_hash,
    want_range, range_window and result belong to the batch runner in prediction_batch.py, which is
    the only thing that creates these; they live here so the ctypes layer can be tested on its own.
    step is deliberately not one of the kernel's inputs - run_prediction_kernel_batch overrides it
    with PREDICT_STEP, so it is only read when a job falls back to the Python engine.
    """

    __slots__ = ("charge_limit", "charge_window", "export_window", "export_limits", "pv_scenario", "end_record", "step", "soc_range_start_step", "soc_range_end_step", "sim_hash", "want_range", "range_window", "result")

    def __init__(self, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, soc_range_start_step=-1, soc_range_end_step=-1, sim_hash=None, want_range=False, range_window=None):
        """Record one job's trial inputs; result stays None until the batch is flushed"""
        self.charge_limit = charge_limit
        self.charge_window = charge_window
        self.export_window = export_window
        self.export_limits = export_limits
        self.pv_scenario = pv_scenario
        self.end_record = end_record
        self.step = step
        self.soc_range_start_step = soc_range_start_step
        self.soc_range_end_step = soc_range_end_step
        self.sim_hash = sim_hash
        self.want_range = want_range
        self.range_window = range_window
        self.result = None


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

    The result is cached module-wide, so the library is loaded and verified once
    per process; failures are logged once and result in a permanent fallback to
    the Python engine.
    """
    global KERNEL_LIB, KERNEL_LOAD_TRIED, KERNEL_STATUS, KERNEL_HAS_BATCH

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
            try:
                lib.pk_run_batch.restype = ctypes.c_int32
                # The trailing c_int32 is n_threads - pk_run_batch takes five arguments, and ctypes
                # will happily pass a fourth-and-a-bit if the binding says otherwise
                lib.pk_run_batch.argtypes = [ctypes.c_int64, ctypes.POINTER(PkBatchJob), ctypes.c_int32, ctypes.POINTER(PkBatchResult), ctypes.c_int32]
                KERNEL_HAS_BATCH = True
            except AttributeError:
                KERNEL_HAS_BATCH = False
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


def select_array_typecode(candidates, ctype):
    """Pick an array.array typecode whose itemsize matches ctype exactly, or None if none does.

    array.array's integer typecodes are C types, so their widths are platform-defined - 'i' is a C
    int, which is 32 bit everywhere predbat runs but is not guaranteed to be. Getting this wrong is
    not a loud failure: from_buffer only checks the buffer is big enough, so a wider backing type is
    accepted and the kernel silently reads interleaved garbage. Matching the size up front, and
    falling back to the slower construction when nothing matches, keeps that impossible.
    """
    size = ctypes.sizeof(ctype)
    for typecode in candidates:
        if array.array(typecode).itemsize == size:
            return typecode
    return None


DOUBLE_TYPECODE = select_array_typecode(("d", "f"), ctypes.c_double)
INT32_TYPECODE = select_array_typecode(("i", "l", "h"), ctypes.c_int32)


def double_array(values):
    """Create a ctypes double array from any iterable of numbers.

    Built via array.array rather than (ctypes.c_double * n)(*values): the latter unpacks the list as
    positional arguments and is several times slower, which matters because these are rebuilt on
    every simulation. from_buffer returns a view over the array.array, and ctypes keeps the backing
    object alive through the view's _objects, so the buffer cannot be collected while the kernel is
    using it. The view is never written to once built: it is filled on the calling thread before
    pk_run/pk_run_batch is entered and the kernel only ever reads it, so even a buffer deliberately
    shared between jobs (the window memo in run_prediction_kernel_batch) is only read concurrently -
    the kernel's worker threads never see a buffer change under them, because the one thread that
    could change it is blocked inside the call.

    An iterable is taken rather than a list so callers can hand over a map/generator and skip
    materialising an intermediate list - array.array consumes it in C and coerces each item to a double
    itself, which is what the caller's float() was doing.
    """
    if DOUBLE_TYPECODE is None:
        values = list(values)
        return (ctypes.c_double * len(values))(*values)
    backing = array.array(DOUBLE_TYPECODE, values)
    return (ctypes.c_double * len(backing)).from_buffer(backing)


def int32_array(values):
    """Create a ctypes int32 array from any iterable of ints - see double_array for why array.array is used"""
    if INT32_TYPECODE is None:
        values = list(values)
        return (ctypes.c_int32 * len(values))(*values)
    backing = array.array(INT32_TYPECODE, values)
    return (ctypes.c_int32 * len(backing)).from_buffer(backing)


# ---------------------------------------------------------------------------
# Window bounds cache
#
# A search re-runs thousands of simulations over the same charge/export windows, changing only the
# limits. The start/end bounds handed to the kernel are therefore identical call after call, but
# were re-derived from the window dicts every time - measured as the largest single block of Python
# time in a plan. They are cached here, keyed on the identity of the window list.
#
# CORRECTNESS: the cache is only sound while no window's start or end changes underneath it. Every
# such mutation must call invalidate_window_cache() - route them through the set_window_start() and
# set_window_end() helpers below rather than assigning window["start"] or window["end"] directly.
# The prediction path does not need them: _prepare_export applies a trial start copy-on-write, to a
# window dict and list of its own, so nothing the cache has seen is touched. run_window_cache_tests replays a full plan with VALIDATE_WINDOW_CACHE on, which
# re-derives the bounds on every hit and fails on any stale entry, so a missed invalidation is a
# test failure rather than a silently wrong plan.
#
# Entries pin their window list (so a freed list cannot have its id() reused by a later
# allocation, which would alias to the wrong entry) and the cache is bounded, so a caller that
# never repeats a list - a forked pool worker, which unpickles fresh lists every call - thrashes
# harmlessly within the bound instead of growing without limit.
# ---------------------------------------------------------------------------
WINDOW_CACHE_MAX = 16
VALIDATE_WINDOW_CACHE = False
WINDOW_CACHE_ENABLED = True
_WINDOW_BOUNDS_CACHE = {}


def disable_window_cache():
    """Turn the window bounds cache off in this process - used as the pool worker initialiser.

    A worker unpickles fresh window lists on every call, so it can never hit the cache; leaving it
    on there only pays the failed lookup. Measured at ~3% of a pooled plan.
    """
    global WINDOW_CACHE_ENABLED
    WINDOW_CACHE_ENABLED = False
    _WINDOW_BOUNDS_CACHE.clear()


def invalidate_window_cache():
    """Drop all cached window bounds - must be called whenever a window's start or end changes"""
    _WINDOW_BOUNDS_CACHE.clear()


def set_window_start(window, start):
    """Set a window's start time, invalidating any cached bounds derived from it.

    Use this instead of assigning window["start"] directly - see the window bounds cache notes
    above for why a bare assignment is unsafe.
    """
    if window["start"] != start:
        window["start"] = start
        _WINDOW_BOUNDS_CACHE.clear()


def set_window_end(window, end):
    """Set a window's end time, invalidating any cached bounds derived from it - see set_window_start"""
    if window["end"] != end:
        window["end"] = end
        _WINDOW_BOUNDS_CACHE.clear()


def _window_cache_entry(window_list):
    """Return the cache entry [window_list, starts, ends, bounds_tuple] for a window list.

    The derived fields start as None and are filled in on first use, so a caller that only wants
    the hash tuple never pays to build the ctypes arrays, and vice versa.
    """
    key = id(window_list)
    entry = _WINDOW_BOUNDS_CACHE.get(key)
    # The window count is part of the hit condition, not just the identity: a list that grows or
    # shrinks in place keeps its id(), and run_prediction_kernel passes n_charge/n_export from
    # len(window_list) alongside these arrays. A stale shorter array would then be read past its end
    # by the C kernel - a memory-safety failure rather than merely a wrong plan. Bare start/end
    # writes are handled by set_window_start/set_window_end instead; nothing on the planning path
    # resizes a window list today, so this is a guard against the class rather than a live fix.
    if entry is not None and entry[0] is window_list and entry[4] == len(window_list):
        if VALIDATE_WINDOW_CACHE:
            _validate_entry(window_list, entry)
        return entry
    if len(_WINDOW_BOUNDS_CACHE) >= WINDOW_CACHE_MAX:
        _WINDOW_BOUNDS_CACHE.clear()
    entry = [window_list, None, None, None, len(window_list)]
    _WINDOW_BOUNDS_CACHE[key] = entry
    return entry


def window_bound_arrays(window_list):
    """Return (start_array, end_array) as ctypes int32 arrays for a window list, cached by identity.

    The returned arrays are shared with other callers and must not be modified.
    """
    if not WINDOW_CACHE_ENABLED:
        return int32_array([window["start"] for window in window_list]), int32_array([window["end"] for window in window_list])
    entry = _window_cache_entry(window_list)
    if entry[1] is None:
        entry[1] = int32_array([window["start"] for window in window_list])
        entry[2] = int32_array([window["end"] for window in window_list])
    return entry[1], entry[2]


def window_bound_tuple(window_list):
    """Return a tuple of (start, end) pairs for a window list, cached by identity (prediction cache key)"""
    if not WINDOW_CACHE_ENABLED:
        return tuple([(window["start"], window["end"]) for window in window_list])
    entry = _window_cache_entry(window_list)
    if entry[3] is None:
        entry[3] = tuple([(window["start"], window["end"]) for window in window_list])
    return entry[3]


def _validate_entry(window_list, entry):
    """Re-derive the bounds and raise if the cached entry is stale (VALIDATE_WINDOW_CACHE only)"""
    starts = [window["start"] for window in window_list]
    ends = [window["end"] for window in window_list]
    if entry[1] is not None and (starts != list(entry[1]) or ends != list(entry[2])):
        raise AssertionError("Stale window bounds cache (arrays): a window changed without invalidate_window_cache().\n  starts cached {} actual {}\n  ends   cached {} actual {}".format(list(entry[1]), starts, list(entry[2]), ends))
    if entry[3] is not None and tuple(zip(starts, ends)) != entry[3]:
        raise AssertionError("Stale window bounds cache (tuple): a window changed without invalidate_window_cache().\n  cached {}\n  actual {}".format(entry[3], tuple(zip(starts, ends))))


def kernel_context_free(handle):
    """Free a kernel context by handle (used as a weakref finaliser)"""
    if KERNEL_LIB and handle:
        KERNEL_LIB.pk_context_free(handle)


def build_static_context_arrays(pred, n_steps, minutes_now, num_cars):
    """Build the per-step context arrays that do not depend on the load forecast.

    Split out from create_kernel_context so a caller building several contexts that differ only in
    their load can compute these once - see the static_cache argument there. Everything here reads
    rates, PV, temperature, carbon, gas, iboost and car data.
    """
    rate_import = []
    rate_export = []
    alert_keep = []
    io_flag = []
    pv = []
    pv10 = []
    pv90 = []
    temp_charge_cap = []
    temp_discharge_cap = []
    carbon = []
    gas_rate = []
    iboost_plan_load = []
    car_load_flat = [0.0] * (num_cars * n_steps)
    car_rate_flat = [0.0] * (num_cars * n_steps)
    # The temperature rate caps are a pure function of the temperature at that step, and a forecast
    # normally holds one value for hours at a time, so the two curve walks are memoised per distinct
    # temperature instead of repeated for every step. On the benchmark that is 17,856 calls per plan
    # for a handful of answers.
    temp_cap_memo = {}
    for k in range(n_steps):
        minute = k * PREDICT_STEP
        minute_absolute = minute + minutes_now
        rate_import.append(pred.rate_import.get(minute_absolute, 0))
        rate_export.append(pred.rate_export.get(minute_absolute, 0))
        alert_keep.append(pred.all_active_keep.get(minute_absolute, 0))
        io_flag.append(1 if pred.io_adjusted.get(minute_absolute, 0) else 0)
        pv.append(pred.pv_forecast_minute_step[minute])
        pv10.append(pred.pv_forecast_minute10_step[minute])
        pv90.append(pred.pv_forecast_minute90_step[minute])
        # Pre-compute the temperature rate cap base (before the min against the max rate,
        # which the kernel applies per lookup) - mirrors utils.py find_battery_temperature_cap
        battery_temperature = pred.battery_temperature_prediction.get(minute, pred.battery_temperature)
        caps = temp_cap_memo.get(battery_temperature)
        if caps is None:
            caps = (
                find_battery_temperature_cap(battery_temperature, pred.battery_temperature_charge_curve, pred.soc_max, float("inf")),
                find_battery_temperature_cap(battery_temperature, pred.battery_temperature_discharge_curve, pred.soc_max, float("inf")),
            )
            temp_cap_memo[battery_temperature] = caps
        temp_charge_cap.append(caps[0])
        temp_discharge_cap.append(caps[1])
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

    return (rate_import, rate_export, alert_keep, io_flag, pv, pv10, pv90, temp_charge_cap, temp_discharge_cap, carbon, gas_rate, iboost_plan_load, car_load_flat, car_rate_flat, charge_curve, discharge_curve)


def create_kernel_context(pred, static_cache=None):
    """Build the per-plan static context for a Prediction object and hand it to the kernel.

    Returns the kernel context handle (>0) or 0 when the kernel is unavailable or
    the context could not be built; 0 means the Python engine will be used.

    static_cache is an opt-in dict for a caller building several contexts that differ ONLY in their
    load forecast - calculate_marginal_costs builds 28 such contexts, one per matrix cell. Passing
    the same dict to each build computes the load-independent arrays once instead of 28 times, which
    is nearly all of the work here. The contract is the caller's to keep: anything else that differs
    between contexts sharing a cache - rates, PV, temperature, carbon, car slots - would be silently
    taken from the first build. Callers with nothing to share pass nothing and get a full build.
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

        # The load arrays are what a static_cache caller varies, so they are always rebuilt here
        load = []
        load10 = []
        load90 = []
        load_step = pred.load_minutes_step
        load10_step = pred.load_minutes_step10
        load90_step = pred.load_minutes_step90
        for k in range(n_steps):
            minute = k * PREDICT_STEP
            load.append(load_step[minute])
            load10.append(load10_step[minute])
            load90.append(load90_step[minute])

        # Keyed on the shape the arrays were built for, so a cache handed to a differently shaped
        # Prediction rebuilds rather than returning arrays of the wrong length
        shape = (n_steps, minutes_now, num_cars)
        static = static_cache.get("arrays") if static_cache is not None else None
        if static is None or static[0] != shape:
            static = (shape, build_static_context_arrays(pred, n_steps, minutes_now, num_cars))
            if static_cache is not None:
                static_cache["arrays"] = static
        (rate_import, rate_export, alert_keep, io_flag, pv, pv10, pv90, temp_charge_cap, temp_discharge_cap, carbon, gas_rate, iboost_plan_load, car_load_flat, car_rate_flat, charge_curve, discharge_curve) = static[1]

        ctx = PkContext()
        ctx.rate_import = double_array(rate_import)
        ctx.rate_export = double_array(rate_export)
        ctx.alert_keep = double_array(alert_keep)
        ctx.pv = double_array(pv)
        ctx.load = double_array(load)
        ctx.pv10 = double_array(pv10)
        ctx.load10 = double_array(load10)
        ctx.pv90 = double_array(pv90)
        ctx.load90 = double_array(load90)
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
        ctx.inverter_freeze_export_discharge_rate = pred.inverter_freeze_export_discharge_rate
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


def kernel_result_tuple(pred, result, predict_soc):
    """Assemble the 17-field Prediction.run_prediction() result tuple from a PkResult.

    Shared by the single-scenario and batch paths - mirrors prediction.py:626-628, 1266-1284.
    """
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


def reset_kernel_run_state(pred):
    """Clear the per-run state attributes exactly as the Python engine does - prediction.py:414-422

    Non-save runs never populate these, so they simply stay empty/False; a kernel run has to leave
    the Prediction in the same state a Python run would.
    """
    pred.predict_soc_best = {}
    pred.predict_metric_best = {}
    pred.predict_iboost_best = {}
    pred.predict_carbon_best = {}
    pred.predict_clipped_best = {}
    pred.iboost_running = False
    pred.iboost_running_solar = False
    pred.iboost_running_full = False


def run_prediction_kernel_batch(pred, jobs, n_threads=1):
    """Run a whole fan-out of prediction scenarios through the kernel in one pk_run_batch call.

    Returns a list of (result_tuple, soc_range_min, soc_range_max) in job order, where result_tuple
    is the same 17-field tuple Prediction.run_prediction() returns, or None for a job the kernel
    refused - the caller re-runs those through the Python engine. Returns None when the batch could
    not be run at all (no kernel, no batch entry point, or a non-zero return code).

    Whether the kernel is allowed to answer at all is the caller's decision, not this function's:
    kernel_supported() also refuses debug and saving runs, and flush_batch checks it before getting
    here.

    Batched jobs never materialise the per-minute SoC series: their callers all discard it, and one
    buffer per job would be ~84MB on a large batch. The one thing a caller does want from it - the
    SoC range across a charge window - the kernel tracks inline over
    [soc_range_start_step, soc_range_end_step] instead.
    """
    lib = KERNEL_LIB
    if not lib or not KERNEL_HAS_BATCH or not getattr(pred, "kernel_handle", 0):
        return None

    n_jobs = len(jobs)
    job_array = (PkBatchJob * n_jobs)()
    result_array = (PkBatchResult * n_jobs)()
    # Every ctypes buffer behind a pointer field has to outlive the call. ctypes' own keepalive already
    # covers this (b_base walks up to job_array._objects), so this list is belt-and-braces: the limit
    # arrays are held explicitly here rather than relying on ctypes' container keepalive.
    buffers = []
    # The charge fan-outs reuse the same window lists across every job, so their start/end arrays are
    # marshalled once per distinct list instead of once per job - which is most of what those batches
    # save. Export windows share this memo only when all_n is truthy; otherwise _prepare_export builds
    # a fresh one per job. Keyed on identity, with the list retained so an id() cannot be recycled
    # mid-batch. This relies on no caller mutating a window list between enqueue and flush (see
    # prediction_batch.py).
    window_cache = {}

    def window_arrays(windows):
        """Marshal a window list's start/end arrays, reusing an earlier job's arrays where possible"""
        entry = window_cache.get(id(windows))
        if entry is None:
            entry = (int32_array([window["start"] for window in windows]), int32_array([window["end"] for window in windows]), windows)
            window_cache[id(windows)] = entry
        return entry

    for index, job in enumerate(jobs):
        charge_start, charge_end, _ = window_arrays(job.charge_window)
        export_start, export_end, _ = window_arrays(job.export_window)
        charge_limit = double_array(job.charge_limit)
        export_limits = double_array(job.export_limits)
        buffers.append((charge_limit, export_limits))

        pk_job = job_array[index]
        pk_job.charge_limit = charge_limit
        pk_job.charge_start = charge_start
        pk_job.charge_end = charge_end
        pk_job.export_limits = export_limits
        pk_job.export_start = export_start
        pk_job.export_end = export_end
        pk_job.soc_out = None
        pk_job.n_charge = len(job.charge_window)
        pk_job.n_export = len(job.export_window)
        pk_job.pv_scenario = int(job.pv_scenario)
        pk_job.end_record = job.end_record
        pk_job.step = PREDICT_STEP  # the caller's step is ignored - see run_prediction_kernel
        pk_job.soc_range_start_step = job.soc_range_start_step
        pk_job.soc_range_end_step = job.soc_range_end_step

    return_code = lib.pk_run_batch(pred.kernel_handle, job_array, n_jobs, result_array, max(int(n_threads), 1))
    if return_code != 0:
        return None

    reset_kernel_run_state(pred)

    results = []
    for index in range(n_jobs):
        batch_result = result_array[index]
        if batch_result.status != 0:
            results.append((None, 0.0, 0.0))
            continue
        results.append((kernel_result_tuple(pred, batch_result.result, {}), batch_result.soc_range_min, batch_result.soc_range_max))
    return results


def run_prediction_kernel(pred, charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step, cache):
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

    # The kernel clips intersecting windows itself (clip_intersecting_charge_windows in
    # prediction_kernel.cpp), mirroring what the Python engine does before simulating. Doing it in
    # Python here cost more per simulation than the simulation, so the raw windows are handed over.

    n_steps = pred.forecast_minutes // PREDICT_STEP
    scenario = PkScenario()
    # The limits are fed to array.array directly: it coerces each item to a double in C, so the float() and
    # the intermediate list the comprehension built are both wasted work (measured 0.89 -> 0.53us per call).
    # The window fields keep their list comprehensions - map(itemgetter(...)) measured *slower* here at these
    # list lengths, the per-item call overhead outweighing what the comprehension costs.
    scenario.charge_limit = double_array(charge_limit)
    scenario.charge_start, scenario.charge_end = window_bound_arrays(charge_window)
    scenario.export_limits = double_array(export_limits)
    scenario.export_start, scenario.export_end = window_bound_arrays(export_window)
    # A cached run discards the per-minute SoC series (see the `if not cache` block below), so the
    # buffer is not allocated and the kernel is told to skip filling it. That skips a round_py per
    # step, which is snprintf+strtod and the single most expensive thing in the kernel's hot loop -
    # measured 52.8us -> 28.5us per scenario, for a value that was being thrown away.
    soc_out = (ctypes.c_double * n_steps)() if not cache else None
    scenario.soc_out = soc_out
    scenario.n_charge = len(charge_window)
    scenario.n_export = len(export_window)
    scenario.pv_scenario = int(pv_scenario)
    scenario.end_record = end_record
    scenario.step = PREDICT_STEP  # the caller's step is ignored - see run_prediction_kernel docstring

    result = PkResult()
    return_code = lib.pk_run(pred.kernel_handle, ctypes.byref(scenario), ctypes.byref(result))
    if return_code != 0:
        return None

    reset_kernel_run_state(pred)

    # Assemble the same return value as the Python engine
    predict_soc = {}
    if not cache:
        # Indexed loop, not dict(zip(range(...), soc_out)): iterating a ctypes array boxes each double
        # through the sequence protocol and measured 34% slower than subscripting it.
        for k in range(n_steps):
            predict_soc[k * PREDICT_STEP] = soc_out[k]

    return kernel_result_tuple(pred, result, predict_soc)
