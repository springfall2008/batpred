# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Numba JIT-Compiled Optimizer Strategy
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=line-too-long

"""
Numba JIT-compiled optimization strategy for faster IOG customer plan times.

This strategy uses Numba to JIT-compile the hot inner loops:
1. Limit computation compiled to machine code
2. Hash computation using fast polynomial rolling hash
3. Warmup phase to trigger compilation before benchmarking

Expected speedup: 2-4x from compiled inner loops

Note: Requires numba package. Falls back gracefully if not available.
"""

from typing import Optional, Callable, Tuple
import numpy as np

from ..base import OptimizerStrategy, OptimizerInput, OptimizerResult, PredictFn

# Try to import numba, provide fallback
try:
    from numba import njit, prange
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    # Provide no-op decorator for fallback
    def njit(*args, **kwargs):
        def decorator(func):
            return func
        return decorator if not args else decorator(args[0])
    prange = range


@njit(cache=True)
def _compute_hash_numba(charge_limits: np.ndarray, export_limits: np.ndarray) -> np.int64:
    """
    Compute a fast polynomial rolling hash for the limit arrays.
    Using Numba-compiled code for speed.
    """
    h = np.int64(17)
    prime = np.int64(31)

    for i in range(len(charge_limits)):
        h = h * prime + np.int64(charge_limits[i] * 1000)

    for i in range(len(export_limits)):
        h = h * prime + np.int64(export_limits[i] * 1000)

    return h


@njit(cache=True)
def _build_trial_limits_numba(
    charge_prices: np.ndarray,
    charge_windows_idx: np.ndarray,
    charge_freeze_flags: np.ndarray,
    export_prices: np.ndarray,
    export_windows_idx: np.ndarray,
    export_freeze_flags: np.ndarray,
    loop_price: float,
    max_charge_slots: int,
    max_export_slots: int,
    try_charge_freeze: bool,
    try_export_freeze: bool,
    reserve: float,
    soc_max: float,
    min_freeze_pct: float,
    num_charge_limits: int,
    num_export_limits: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    JIT-compiled function to build trial limits.
    """
    try_charge_limit = np.zeros(num_charge_limits, dtype=np.float64)
    try_export = np.full(num_export_limits, 100.0, dtype=np.float64)

    # Track used windows with simple arrays (no Python sets in numba)
    used_charge = np.zeros(num_charge_limits, dtype=np.bool_)
    used_export = np.zeros(num_export_limits, dtype=np.bool_)

    count_c = 0
    count_d = 0

    # Apply charge windows
    for i in range(len(charge_prices)):
        if charge_prices[i] <= loop_price:
            window_n = charge_windows_idx[i]
            freeze = charge_freeze_flags[i]
            if freeze and not try_charge_freeze:
                continue
            if count_c < max_charge_slots and not used_charge[window_n]:
                used_charge[window_n] = True
                try_charge_limit[window_n] = reserve if freeze else soc_max
                count_c += 1

    # Apply export windows
    for i in range(len(export_prices)):
        if export_prices[i] > loop_price:
            window_n = export_windows_idx[i]
            freeze = export_freeze_flags[i]
            if freeze and not try_export_freeze:
                continue
            if count_d < max_export_slots and not used_export[window_n]:
                used_export[window_n] = True
                try_export[window_n] = 99.0 if freeze else min_freeze_pct
                count_d += 1

    return try_charge_limit, try_export


class NumbaJITStrategy(OptimizerStrategy):
    """
    Numba JIT-compiled optimization strategy.

    Uses JIT-compiled inner loops for building trial limits and computing hashes.
    Requires numba package for full performance benefit.
    """

    SLOT_OPTIONS = [48, 32, 24, 16, 12, 8, 6, 5, 4, 3, 2, 1]

    @property
    def name(self) -> str:
        return "numba_jit"

    @property
    def description(self) -> str:
        return f"Numba JIT-compiled inner loops (numba available: {NUMBA_AVAILABLE})"

    def __init__(self, warmup: bool = True):
        """
        Initialize the Numba JIT strategy.

        Args:
            warmup: If True, trigger JIT compilation on init to avoid
                   timing compilation in the first benchmark run.
        """
        self._warmed_up = False
        if warmup and NUMBA_AVAILABLE:
            self._warmup()

    def _warmup(self):
        """Trigger JIT compilation with small test data."""
        if self._warmed_up:
            return

        # Small test arrays to trigger compilation
        test_charge = np.array([0.1, 0.2], dtype=np.float64)
        test_export = np.array([100.0], dtype=np.float64)
        test_prices = np.array([10.0], dtype=np.float64)
        test_idx = np.array([0], dtype=np.int32)
        test_flags = np.array([False], dtype=np.bool_)

        # Trigger compilation
        _compute_hash_numba(test_charge, test_export)
        _build_trial_limits_numba(
            test_prices, test_idx, test_flags,
            test_prices, test_idx, test_flags,
            15.0, 4, 4, False, False,
            0.5, 10.0, 10.0, 2, 1
        )

        self._warmed_up = True

    def optimize(
        self,
        inputs: OptimizerInput,
        predict_fn: PredictFn,
        launch_predict_fn: Optional[Callable] = None,
    ) -> OptimizerResult:
        # Ensure warmup is done
        if not self._warmed_up and NUMBA_AVAILABLE:
            self._warmup()

        iterations = 0
        predictions_run = 0

        best_metric = 9999999
        best_cost = 0
        best_keep = 0
        best_soc_min = inputs.battery.soc_max
        best_soc_min_minute = 0
        best_cycle = 0
        best_carbon = 0
        best_import = 0

        best_charge_limits = inputs.charge_limits.copy()
        best_export_limits = inputs.export_limits.copy()

        tried_list = inputs.tried_list.copy() if inputs.tried_list else {}
        levels_score = inputs.levels_score.copy() if inputs.levels_score else {}
        best_level_score = None
        level_score_range = 1.0

        charge_freeze_options = [True, False] if inputs.config.set_charge_freeze else [False]
        export_freeze_options = [True, False] if inputs.config.set_export_freeze else [False]

        # Build price sets
        price_set_charge = []
        valid_charge_windows = {}

        for price in inputs.price_set:
            links = inputs.price_links.get(price, [])
            for key in links:
                window_info = inputs.window_index.get(key, {})
                typ = window_info.get("type", "")
                window_n = window_info.get("id", -1)
                if typ in ["c", "cf"] and window_n >= 0 and window_n < len(inputs.charge_windows):
                    window = inputs.charge_windows[window_n]
                    if inputs.region_start is not None:
                        if window["start"] >= inputs.region_end or window["end"] < inputs.region_start:
                            continue
                    if window_n not in valid_charge_windows:
                        price_set_charge.append((price, window_n, typ == "cf"))
                        valid_charge_windows[window_n] = True

        price_set_export = []
        valid_export_windows = {}

        for price in inputs.price_set:
            links = inputs.price_links.get(price, [])
            for key in links:
                window_info = inputs.window_index.get(key, {})
                typ = window_info.get("type", "")
                window_n = window_info.get("id", -1)
                if typ in ["d", "df"] and window_n >= 0 and window_n < len(inputs.export_windows):
                    window = inputs.export_windows[window_n]
                    if inputs.region_start is not None:
                        if window["start"] >= inputs.region_end or window["end"] < inputs.region_start:
                            continue
                    if window_n not in valid_export_windows:
                        price_set_export.append((price, window_n, typ == "df"))
                        valid_export_windows[window_n] = True

        # Convert to numpy arrays for Numba
        if price_set_charge:
            charge_prices = np.array([p[0] for p in price_set_charge], dtype=np.float64)
            charge_windows_idx = np.array([p[1] for p in price_set_charge], dtype=np.int32)
            charge_freeze_flags = np.array([p[2] for p in price_set_charge], dtype=np.bool_)
        else:
            charge_prices = np.array([], dtype=np.float64)
            charge_windows_idx = np.array([], dtype=np.int32)
            charge_freeze_flags = np.array([], dtype=np.bool_)

        if price_set_export:
            export_prices = np.array([p[0] for p in price_set_export], dtype=np.float64)
            export_windows_idx = np.array([p[1] for p in price_set_export], dtype=np.int32)
            export_freeze_flags = np.array([p[2] for p in price_set_export], dtype=np.bool_)
        else:
            export_prices = np.array([], dtype=np.float64)
            export_windows_idx = np.array([], dtype=np.int32)
            export_freeze_flags = np.array([], dtype=np.bool_)

        # Pre-compute constants
        reserve = inputs.battery.reserve
        soc_max = inputs.battery.soc_max
        min_freeze_pct = (inputs.config.best_soc_min / soc_max) * 100
        num_charge_limits = len(inputs.charge_limits)
        num_export_limits = len(inputs.export_limits)

        for loop_price in inputs.all_prices:
            if best_level_score is not None:
                this_level_score = levels_score.get(loop_price, 9999999)
                if abs(this_level_score - best_level_score) > (0.3 * level_score_range):
                    continue

            for max_charge_slots in self.SLOT_OPTIONS:
                for max_export_slots in self.SLOT_OPTIONS:
                    for try_charge_freeze in charge_freeze_options:
                        for try_export_freeze in export_freeze_options:
                            iterations += 1

                            # Use JIT-compiled function to build limits
                            try_charge_limit, try_export = _build_trial_limits_numba(
                                charge_prices, charge_windows_idx, charge_freeze_flags,
                                export_prices, export_windows_idx, export_freeze_flags,
                                loop_price, max_charge_slots, max_export_slots,
                                try_charge_freeze, try_export_freeze,
                                reserve, soc_max, min_freeze_pct,
                                num_charge_limits, num_export_limits
                            )

                            # Use JIT-compiled hash
                            try_hash = _compute_hash_numba(try_charge_limit, try_export)

                            if try_hash in tried_list:
                                cached_value = tried_list[try_hash]
                                if cached_value is not True:
                                    if loop_price not in levels_score:
                                        levels_score[loop_price] = 9999999
                                    levels_score[loop_price] = min(levels_score[loop_price], cached_value)
                                continue

                            tried_list[try_hash] = True

                            end_record = inputs.end_record or inputs.config.forecast_minutes
                            step = inputs.config.step

                            # Convert to lists for predict_fn
                            charge_list = try_charge_limit.tolist()
                            export_list = try_export.tolist()

                            result = predict_fn(
                                charge_list, inputs.charge_windows, inputs.export_windows,
                                export_list, False, end_record, step
                            )
                            predictions_run += 1

                            result10 = predict_fn(
                                charge_list, inputs.charge_windows, inputs.export_windows,
                                export_list, True, end_record, step
                            )
                            predictions_run += 1

                            (cost, import_kwh_battery, import_kwh_house, export_kwh,
                             soc_min, soc, soc_min_minute, battery_cycle,
                             metric_keep, final_iboost, final_carbon_g) = result

                            (cost10, import_kwh_battery10, import_kwh_house10, export_kwh10,
                             soc_min10, soc10, soc_min_minute10, battery_cycle10,
                             metric_keep10, final_iboost10, final_carbon_g10) = result10

                            metric = (cost + cost10) / 2
                            metric += battery_cycle * inputs.config.metric_battery_cycle
                            metric += metric_keep

                            tried_list[try_hash] = metric

                            if loop_price not in levels_score:
                                levels_score[loop_price] = 9999999
                            levels_score[loop_price] = min(levels_score[loop_price], metric)

                            if metric < best_metric:
                                best_metric = metric
                                best_cost = cost
                                best_keep = metric_keep
                                best_soc_min = min(soc_min, soc_min10)
                                best_soc_min_minute = soc_min_minute
                                best_cycle = battery_cycle
                                best_carbon = final_carbon_g
                                best_import = import_kwh_battery + import_kwh_house
                                best_charge_limits = charge_list.copy()
                                best_export_limits = export_list.copy()

                                if best_level_score is None:
                                    best_level_score = metric
                                else:
                                    best_level_score = min(best_level_score, metric)

            if levels_score:
                scores = list(levels_score.values())
                level_score_range = max(scores) - min(scores) if len(scores) > 1 else 1.0

        return OptimizerResult(
            best_metric=best_metric,
            best_cost=best_cost,
            best_keep=best_keep,
            best_soc_min=best_soc_min,
            best_soc_min_minute=best_soc_min_minute,
            best_cycle=best_cycle,
            best_carbon=best_carbon,
            best_import=best_import,
            charge_limits=best_charge_limits,
            export_limits=best_export_limits,
            iterations=iterations,
            predictions_run=predictions_run,
            elapsed_seconds=0.0,
            strategy_name=self.name,
            tried_list=tried_list,
            levels_score=levels_score,
        )
