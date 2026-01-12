# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Coarse-to-Fine Optimizer Strategy
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=line-too-long

"""
Coarse-to-Fine optimization strategy for faster IOG customer plan times.

This strategy reduces the 576 combinations (12x12x2x2) per price level to
approximately 64 + 27 = 91 combinations through a two-stage search:

Stage 1 (Coarse): Search with reduced slot options [48, 16, 4, 1]
  - 4x4x2x2 = 64 combinations (vs 576)
  - Finds approximate best region in search space

Stage 2 (Fine): Refine around best coarse solution
  - Uses neighboring values from full grid [48, 32, 24, 16, 12, 8, 6, 5, 4, 3, 2, 1]
  - ~3x3x2x2 = ~36 combinations (with deduplication ~27)
  - Finds optimal solution near coarse best

Expected speedup: 4-5x from reduced combinations (91 vs 576 per price level)
"""

from typing import Optional, Callable, List, Tuple
from ..base import OptimizerStrategy, OptimizerInput, OptimizerResult, PredictFn


class CoarseToFineStrategy(OptimizerStrategy):
    """
    Coarse-to-Fine optimization strategy.

    Uses a two-stage search to dramatically reduce the number of combinations
    evaluated while maintaining near-optimal results.
    """

    COARSE_SLOTS = [48, 16, 4, 1]
    FULL_SLOTS = [48, 32, 24, 16, 12, 8, 6, 5, 4, 3, 2, 1]

    @property
    def name(self) -> str:
        return "coarse_fine"

    @property
    def description(self) -> str:
        return "Two-stage coarse-to-fine search (4x4 coarse + 3x3 refinement)"

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

        charge_freeze_options = [True, False] if inputs.config.set_charge_freeze else [False]
        export_freeze_options = [True, False] if inputs.config.set_export_freeze else [False]

        price_set_charge = []
        valid_charge_windows = {}
        best_limits_reset = [0.0] * len(inputs.charge_limits)

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
                        price_set_charge.append([price, window_n, typ == "cf"])
                        valid_charge_windows[window_n] = True
                        best_limits_reset[window_n] = 0

        price_set_export = []
        valid_export_windows = {}
        best_export_limits_reset = [100.0] * len(inputs.export_limits)

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
                        price_set_export.append([price, window_n, typ == "df"])
                        valid_export_windows[window_n] = True
                        best_export_limits_reset[window_n] = 100.0

        for loop_price in inputs.all_prices:
            if best_level_score is not None:
                this_level_score = levels_score.get(loop_price, 9999999)
                if abs(this_level_score - best_level_score) > (0.3 * level_score_range):
                    continue

            # Stage 1: Coarse search
            coarse_best_charge_slot = self.COARSE_SLOTS[0]
            coarse_best_export_slot = self.COARSE_SLOTS[0]
            coarse_best_metric = 9999999

            for max_charge_slots in self.COARSE_SLOTS:
                for max_export_slots in self.COARSE_SLOTS:
                    for try_charge_freeze in charge_freeze_options:
                        for try_export_freeze in export_freeze_options:
                            iterations += 1
                            result_tuple = self._evaluate_combination(
                                inputs, loop_price, max_charge_slots, max_export_slots,
                                try_charge_freeze, try_export_freeze,
                                price_set_charge, price_set_export,
                                best_limits_reset, best_export_limits_reset,
                                tried_list, levels_score, predict_fn,
                            )
                            if result_tuple is None:
                                continue

                            (metric, cost, keep, soc_min, soc_min_minute, cycle, carbon,
                             import_total, try_charge_limit, try_export, preds_used) = result_tuple
                            predictions_run += preds_used

                            if metric < coarse_best_metric:
                                coarse_best_metric = metric
                                coarse_best_charge_slot = max_charge_slots
                                coarse_best_export_slot = max_export_slots

                            if metric < best_metric:
                                best_metric = metric
                                best_cost = cost
                                best_keep = keep
                                best_soc_min = soc_min
                                best_soc_min_minute = soc_min_minute
                                best_cycle = cycle
                                best_carbon = carbon
                                best_import = import_total
                                best_charge_limits = try_charge_limit.copy()
                                best_export_limits = try_export.copy()
                                if best_level_score is None:
                                    best_level_score = metric
                                else:
                                    best_level_score = min(best_level_score, metric)

            # Stage 2: Fine search
            fine_charge_slots = self._get_neighbors(coarse_best_charge_slot)
            fine_export_slots = self._get_neighbors(coarse_best_export_slot)

            for max_charge_slots in fine_charge_slots:
                for max_export_slots in fine_export_slots:
                    for try_charge_freeze in charge_freeze_options:
                        for try_export_freeze in export_freeze_options:
                            iterations += 1
                            result_tuple = self._evaluate_combination(
                                inputs, loop_price, max_charge_slots, max_export_slots,
                                try_charge_freeze, try_export_freeze,
                                price_set_charge, price_set_export,
                                best_limits_reset, best_export_limits_reset,
                                tried_list, levels_score, predict_fn,
                            )
                            if result_tuple is None:
                                continue

                            (metric, cost, keep, soc_min, soc_min_minute, cycle, carbon,
                             import_total, try_charge_limit, try_export, preds_used) = result_tuple
                            predictions_run += preds_used

                            if metric < best_metric:
                                best_metric = metric
                                best_cost = cost
                                best_keep = keep
                                best_soc_min = soc_min
                                best_soc_min_minute = soc_min_minute
                                best_cycle = cycle
                                best_carbon = carbon
                                best_import = import_total
                                best_charge_limits = try_charge_limit.copy()
                                best_export_limits = try_export.copy()
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

    def _get_neighbors(self, value: int) -> List[int]:
        if value in self.FULL_SLOTS:
            idx = self.FULL_SLOTS.index(value)
        else:
            idx = 0
            min_diff = abs(self.FULL_SLOTS[0] - value)
            for i, slot in enumerate(self.FULL_SLOTS):
                diff = abs(slot - value)
                if diff < min_diff:
                    min_diff = diff
                    idx = i
        start = max(0, idx - 1)
        end = min(len(self.FULL_SLOTS), idx + 2)
        return self.FULL_SLOTS[start:end]

    def _evaluate_combination(
        self, inputs, loop_price, max_charge_slots, max_export_slots,
        try_charge_freeze, try_export_freeze,
        price_set_charge, price_set_export,
        best_limits_reset, best_export_limits_reset,
        tried_list, levels_score, predict_fn,
    ) -> Optional[Tuple]:
        try_charge_limit = best_limits_reset.copy()
        try_export = best_export_limits_reset.copy()

        all_n = []
        all_d = []
        count_c = 0
        count_d = 0

        for price, window_n, freeze in price_set_charge:
            if loop_price >= price:
                if freeze and not try_charge_freeze:
                    continue
                if count_c < max_charge_slots and window_n not in all_n:
                    all_n.append(window_n)
                    try_charge_limit[window_n] = inputs.battery.reserve if freeze else inputs.battery.soc_max
                    count_c += 1

        for price, window_n, freeze in price_set_export:
            if loop_price < price:
                if freeze and not try_export_freeze:
                    continue
                if count_d < max_export_slots and window_n not in all_d:
                    all_d.append(window_n)
                    min_freeze_pct = (inputs.config.best_soc_min / inputs.battery.soc_max) * 100
                    try_export[window_n] = 99.0 if freeze else min_freeze_pct
                    count_d += 1

        try_hash = hash((tuple(try_charge_limit), tuple(try_export)))
        if try_hash in tried_list:
            cached_value = tried_list[try_hash]
            if cached_value is not True:
                if loop_price not in levels_score:
                    levels_score[loop_price] = 9999999
                levels_score[loop_price] = min(levels_score[loop_price], cached_value)
            return None

        tried_list[try_hash] = True

        end_record = inputs.end_record or inputs.config.forecast_minutes
        step = inputs.config.step
        predictions_run = 0

        result = predict_fn(try_charge_limit, inputs.charge_windows, inputs.export_windows,
                           try_export, False, end_record, step)
        predictions_run += 1

        result10 = predict_fn(try_charge_limit, inputs.charge_windows, inputs.export_windows,
                             try_export, True, end_record, step)
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

        combined_soc_min = min(soc_min, soc_min10)
        import_total = import_kwh_battery + import_kwh_house

        return (metric, cost, metric_keep, combined_soc_min, soc_min_minute,
                battery_cycle, final_carbon_g, import_total, try_charge_limit,
                try_export, predictions_run)
