# -----------------------------------------------------------------------------
# Predbat Home Battery System
# NumPy Vectorized Optimizer Strategy
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=line-too-long

"""
NumPy-vectorized optimization strategy for faster IOG customer plan times.

This strategy uses NumPy arrays to:
1. Pre-compute all 576 combinations (12x12x2x2) as arrays
2. Batch-compute trial limits for all combinations
3. Use efficient hash computation for deduplication

Expected speedup: 1.5-2x from reduced Python overhead
"""

from typing import Optional, Callable
import numpy as np

from ..base import OptimizerStrategy, OptimizerInput, OptimizerResult, PredictFn


class NumpyVectorizedStrategy(OptimizerStrategy):
    """
    NumPy-vectorized optimization strategy.

    Pre-computes all slot combinations and uses vectorized operations
    to reduce Python loop overhead.
    """

    SLOT_OPTIONS = np.array([48, 32, 24, 16, 12, 8, 6, 5, 4, 3, 2, 1], dtype=np.int32)

    @property
    def name(self) -> str:
        return "numpy_vec"

    @property
    def description(self) -> str:
        return "NumPy-vectorized pre-computed combinations"

    def __init__(self):
        self._combinations_cache = {}

    def _get_combinations(self, has_charge_freeze: bool, has_export_freeze: bool) -> np.ndarray:
        """
        Get pre-computed combinations array for the given freeze options.
        Returns array of shape (N, 4): [max_charge_slots, max_export_slots, charge_freeze, export_freeze]
        """
        cache_key = (has_charge_freeze, has_export_freeze)
        if cache_key in self._combinations_cache:
            return self._combinations_cache[cache_key]

        charge_freeze_opts = np.array([1, 0] if has_charge_freeze else [0], dtype=np.int32)
        export_freeze_opts = np.array([1, 0] if has_export_freeze else [0], dtype=np.int32)

        cs, es, cf, ef = np.meshgrid(
            self.SLOT_OPTIONS, self.SLOT_OPTIONS, charge_freeze_opts, export_freeze_opts, indexing='ij'
        )

        combinations = np.stack([cs.ravel(), es.ravel(), cf.ravel(), ef.ravel()], axis=1)
        self._combinations_cache[cache_key] = combinations
        return combinations

    def optimize(
        self,
        inputs: OptimizerInput,
        predict_fn: PredictFn,
        launch_predict_fn: Optional[Callable] = None,
    ) -> OptimizerResult:
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

        # Build price sets for charge and export
        price_set_charge = []
        valid_charge_windows = {}
        best_limits_reset = np.zeros(len(inputs.charge_limits), dtype=np.float64)

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
        best_export_limits_reset = np.full(len(inputs.export_limits), 100.0, dtype=np.float64)

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

        # Convert to numpy arrays for faster access
        charge_prices = np.array([p[0] for p in price_set_charge], dtype=np.float64) if price_set_charge else np.array([], dtype=np.float64)
        charge_windows_idx = np.array([p[1] for p in price_set_charge], dtype=np.int32) if price_set_charge else np.array([], dtype=np.int32)
        charge_freeze_flags = np.array([p[2] for p in price_set_charge], dtype=np.bool_) if price_set_charge else np.array([], dtype=np.bool_)

        export_prices = np.array([p[0] for p in price_set_export], dtype=np.float64) if price_set_export else np.array([], dtype=np.float64)
        export_windows_idx = np.array([p[1] for p in price_set_export], dtype=np.int32) if price_set_export else np.array([], dtype=np.int32)
        export_freeze_flags = np.array([p[2] for p in price_set_export], dtype=np.bool_) if price_set_export else np.array([], dtype=np.bool_)

        reserve = inputs.battery.reserve
        soc_max = inputs.battery.soc_max
        min_freeze_pct = (inputs.config.best_soc_min / soc_max) * 100

        combinations = self._get_combinations(inputs.config.set_charge_freeze, inputs.config.set_export_freeze)

        for loop_price in inputs.all_prices:
            if best_level_score is not None:
                this_level_score = levels_score.get(loop_price, 9999999)
                if abs(this_level_score - best_level_score) > (0.3 * level_score_range):
                    continue

            for combo in combinations:
                max_charge_slots = int(combo[0])
                max_export_slots = int(combo[1])
                try_charge_freeze = bool(combo[2])
                try_export_freeze = bool(combo[3])

                iterations += 1

                try_charge_limit = best_limits_reset.copy()
                try_export = best_export_limits_reset.copy()

                count_c = 0
                used_charge = set()
                if len(charge_prices) > 0:
                    mask_charge = charge_prices <= loop_price
                    for i in np.where(mask_charge)[0]:
                        window_n = charge_windows_idx[i]
                        freeze = charge_freeze_flags[i]
                        if freeze and not try_charge_freeze:
                            continue
                        if count_c < max_charge_slots and window_n not in used_charge:
                            used_charge.add(window_n)
                            try_charge_limit[window_n] = reserve if freeze else soc_max
                            count_c += 1

                count_d = 0
                used_export = set()
                if len(export_prices) > 0:
                    mask_export = export_prices > loop_price
                    for i in np.where(mask_export)[0]:
                        window_n = export_windows_idx[i]
                        freeze = export_freeze_flags[i]
                        if freeze and not try_export_freeze:
                            continue
                        if count_d < max_export_slots and window_n not in used_export:
                            used_export.add(window_n)
                            try_export[window_n] = 99.0 if freeze else min_freeze_pct
                            count_d += 1

                try_hash = hash((tuple(try_charge_limit), tuple(try_export)))
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

                charge_list = try_charge_limit.tolist()
                export_list = try_export.tolist()

                result = predict_fn(charge_list, inputs.charge_windows, inputs.export_windows, export_list, False, end_record, step)
                predictions_run += 1

                result10 = predict_fn(charge_list, inputs.charge_windows, inputs.export_windows, export_list, True, end_record, step)
                predictions_run += 1

                (cost, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g) = result
                (cost10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10, battery_cycle10, metric_keep10, final_iboost10, final_carbon_g10) = result10

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
