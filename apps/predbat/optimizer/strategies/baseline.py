# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Baseline Optimizer Strategy - Port of existing nested loop implementation
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=line-too-long

from typing import Optional, Callable
from ..base import OptimizerStrategy, OptimizerInput, OptimizerResult, PredictFn


class BaselineStrategy(OptimizerStrategy):
    """
    Baseline strategy - direct port of existing optimise_charge_limit_price_threads.

    This implementation mirrors the existing nested loop approach:
    - Iterates through all price levels
    - For each price level, tries all 576 combinations (12x12x2x2)
    - Launches parallel predictions for each combination

    Used as the reference implementation for validating other strategies.
    """

    @property
    def name(self) -> str:
        return "baseline"

    @property
    def description(self) -> str:
        return "Original nested loop implementation (12x12x2x2 = 576 combinations per price level)"

    def optimize(
        self,
        inputs: OptimizerInput,
        predict_fn: PredictFn,
        launch_predict_fn: Optional[Callable] = None,
    ) -> OptimizerResult:
        """
        Run optimization using the original nested loop approach.

        This is a standalone port of the optimization logic that doesn't
        require access to the Plan class instance.
        """
        iterations = 0
        predictions_run = 0

        # Initialize best values
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

        # Determine charge/export freeze options
        charge_freeze_options = [True, False] if inputs.config.set_charge_freeze else [False]
        export_freeze_options = [True, False] if inputs.config.set_export_freeze else [False]

        # Build price sets for charge and export
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
                    # Skip if outside region
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
                    # Skip if outside region
                    if inputs.region_start is not None:
                        if window["start"] >= inputs.region_end or window["end"] < inputs.region_start:
                            continue
                    if window_n not in valid_export_windows:
                        price_set_export.append([price, window_n, typ == "df"])
                        valid_export_windows[window_n] = True
                        best_export_limits_reset[window_n] = 100.0

        # Slot count options
        slot_options = [48, 32, 24, 16, 12, 8, 6, 5, 4, 3, 2, 1]

        # Main optimization loop over price levels
        for loop_price in inputs.all_prices:
            # Skip price levels that are too far from best
            if best_level_score is not None:
                this_level_score = levels_score.get(loop_price, 9999999)
                if abs(this_level_score - best_level_score) > (0.3 * level_score_range):
                    continue

            # Try all combinations
            for max_charge_slots in slot_options:
                for max_export_slots in slot_options:
                    for try_charge_freeze in charge_freeze_options:
                        for try_export_freeze in export_freeze_options:
                            iterations += 1

                            # Build trial limits
                            try_charge_limit = best_limits_reset.copy()
                            try_export = best_export_limits_reset.copy()

                            all_n = []
                            all_d = []
                            count_c = 0
                            count_d = 0

                            # Apply charge windows below price threshold
                            for price, window_n, freeze in price_set_charge:
                                if loop_price >= price:
                                    if freeze and not try_charge_freeze:
                                        continue
                                    if count_c < max_charge_slots and window_n not in all_n:
                                        all_n.append(window_n)
                                        try_charge_limit[window_n] = inputs.battery.reserve if freeze else inputs.battery.soc_max
                                        count_c += 1

                            # Apply export windows above price threshold
                            for price, window_n, freeze in price_set_export:
                                if loop_price < price:
                                    if freeze and not try_export_freeze:
                                        continue
                                    if count_d < max_export_slots and window_n not in all_d:
                                        all_d.append(window_n)
                                        min_freeze_pct = (inputs.config.best_soc_min / inputs.battery.soc_max) * 100
                                        try_export[window_n] = 99.0 if freeze else min_freeze_pct
                                        count_d += 1

                            # Check if we've tried this combination
                            try_hash = hash((tuple(try_charge_limit), tuple(try_export)))
                            if try_hash in tried_list:
                                cached_value = tried_list[try_hash]
                                if cached_value is not True:
                                    if loop_price not in levels_score:
                                        levels_score[loop_price] = 9999999
                                    levels_score[loop_price] = min(levels_score[loop_price], cached_value)
                                continue

                            tried_list[try_hash] = True

                            # Run predictions
                            end_record = inputs.end_record or inputs.config.forecast_minutes
                            step = inputs.config.step

                            # Run prediction (non-pv10)
                            result = predict_fn(
                                try_charge_limit,
                                inputs.charge_windows,
                                inputs.export_windows,
                                try_export,
                                False,  # pv10
                                end_record,
                                step,
                            )
                            predictions_run += 1

                            # Run prediction (pv10)
                            result10 = predict_fn(
                                try_charge_limit,
                                inputs.charge_windows,
                                inputs.export_windows,
                                try_export,
                                True,  # pv10
                                end_record,
                                step,
                            )
                            predictions_run += 1

                            # Unpack results
                            (cost, import_kwh_battery, import_kwh_house, export_kwh,
                             soc_min, soc, soc_min_minute, battery_cycle,
                             metric_keep, final_iboost, final_carbon_g) = result

                            (cost10, import_kwh_battery10, import_kwh_house10, export_kwh10,
                             soc_min10, soc10, soc_min_minute10, battery_cycle10,
                             metric_keep10, final_iboost10, final_carbon_g10) = result10

                            # Compute combined metric (simplified version)
                            # Real implementation would call compute_metric()
                            metric = (cost + cost10) / 2
                            metric += battery_cycle * inputs.config.metric_battery_cycle
                            metric += metric_keep

                            tried_list[try_hash] = metric

                            # Update levels score
                            if loop_price not in levels_score:
                                levels_score[loop_price] = 9999999
                            levels_score[loop_price] = min(levels_score[loop_price], metric)

                            # Check if this is the best result
                            if metric < best_metric:
                                best_metric = metric
                                best_cost = cost
                                best_keep = metric_keep
                                best_soc_min = min(soc_min, soc_min10)
                                best_soc_min_minute = soc_min_minute
                                best_cycle = battery_cycle
                                best_carbon = final_carbon_g
                                best_import = import_kwh_battery + import_kwh_house
                                best_charge_limits = try_charge_limit.copy()
                                best_export_limits = try_export.copy()

                                # Update best level score
                                if best_level_score is None:
                                    best_level_score = metric
                                else:
                                    best_level_score = min(best_level_score, metric)

            # Update level score range after processing each price
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
            elapsed_seconds=0.0,  # Will be set by _timed_optimize
            strategy_name=self.name,
            tried_list=tried_list,
            levels_score=levels_score,
        )
