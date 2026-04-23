# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Marginal energy cost calculation.

Computes a matrix of marginal energy costs by running what-if predictions
with extra load injected into different upcoming time windows.
The matrix covers extra-kWh levels across multiple time windows.
"""

import copy
from datetime import timedelta
from const import PREDICT_STEP
from prediction import Prediction
from utils import dp2

MARGINAL_EXTRA_KWH_LEVELS = [1, 2, 4, 8]
MARGINAL_EXTRA_KWH_LEVEL_NAMES = ["low", "med", "high", "ev"]  # Example labels for the extra load levels
MARGINAL_TIME_OFFSETS = [0, 2 * 60, 4 * 60, 6 * 60, 8 * 60, 10 * 60, 12 * 60]


class Marginal:
    """Marginal energy cost calculation mixin.

    Runs what-if scenarios to compute the marginal cost (p/kWh) of consuming
    extra electricity across different upcoming time windows, using the current
    best charge/discharge plan. Results are stored in a matrix and published
    to a Home Assistant sensor.
    """

    def calculate_marginal_costs(self):
        """Calculate the marginal energy cost matrix.

        Runs an internal baseline prediction (save=None, so standing charges are
        excluded) against the current best plan, then runs N what-if simulations
        with the same save=None conditions.
        Taking the delta against the internal baseline ensures both sides are
        computed consistently without the standing-charge offset that is only
        added when save='best'/'base'.

        Results are stored in:
          self.marginal_costs_matrix  - nested dict {kWh_label: {time_label: cost}}
          self.marginal_costs_min     - minimum value across all cells
          self.marginal_costs_max     - maximum value across all cells
          self.marginal_baseline_metric - baseline metric used for delta calculations
        """
        # Run a baseline prediction with the original (unmodified) load and save=None
        # so standing charges are NOT included — matching the what-if simulations exactly.
        (baseline_metric, *_) = self.prediction.run_prediction(
            self.charge_limit_best,
            self.charge_window_best,
            self.export_window_best,
            self.export_limits_best,
            False,
            self.end_record,
        )
        self.marginal_baseline_metric = baseline_metric
        self.log("Marginal costs baseline metric (no standing charge): {}".format(baseline_metric))
        # Compute actual wall-clock HH:MM labels for each time offset
        time_labels = [(self.now_utc + timedelta(minutes=offset)).strftime("%H:%M") for offset in MARGINAL_TIME_OFFSETS]
        self.marginal_time_labels = time_labels
        matrix = {}
        all_costs = []

        for extra_kwh in MARGINAL_EXTRA_KWH_LEVELS:
            matrix[extra_kwh] = {}
            # Convert kWh/hour extra load to kWh per 5-minute step
            extra_per_step = extra_kwh * PREDICT_STEP / 60.0

            for idx, offset in enumerate(MARGINAL_TIME_OFFSETS):
                time_label = time_labels[idx]

                # Deep-copy both load forecasts to avoid mutating shared data
                modified_load = copy.deepcopy(self.load_minutes_step)

                # Inject extra load into the 1-hour window starting at `offset` minutes from now
                for minute in range(offset, offset + 60, PREDICT_STEP):
                    if minute in modified_load:
                        modified_load[minute] += extra_per_step

                # Create a fresh Prediction with the modified load; this updates PRED_GLOBAL
                # which is safe since we run synchronously (pool is idle at this point)
                # No need to include 10% extra load as we only run normal simulations.
                pred = Prediction(self, self.pv_forecast_minute_step, self.pv_forecast_minute_step, modified_load, modified_load)

                # Run prediction against the current best charge/discharge plan, no save to HA
                (new_metric, *_) = pred.run_prediction(
                    self.charge_limit_best,
                    self.charge_window_best,
                    self.export_window_best,
                    self.export_limits_best,
                    False,
                    self.end_record,
                )

                # Marginal cost in currency units per kWh = delta metric / extra kWh
                marginal_cost = dp2((new_metric - baseline_metric) / extra_kwh)
                matrix[extra_kwh][time_label] = marginal_cost
                all_costs.append(marginal_cost)

        self.marginal_costs_matrix = matrix
        self.marginal_costs_min = min(all_costs) if all_costs else 0.0
        self.marginal_costs_max = max(all_costs) if all_costs else 0.0
        self.log("Marginal energy costs matrix computed: min={} max={}".format(self.marginal_costs_min, self.marginal_costs_max))

        # Record actual grid import/export rate at each time window
        grid_import = {}
        grid_export = {}
        for idx, offset in enumerate(MARGINAL_TIME_OFFSETS):
            minute = self.minutes_now + offset
            grid_import[time_labels[idx]] = dp2(self.rate_import.get(minute, 0))
            grid_export[time_labels[idx]] = dp2(self.rate_export.get(minute, 0))
        self.marginal_grid_import = grid_import
        self.marginal_grid_export = grid_export
        self.publish_marginal_costs()

    def publish_marginal_costs(self):
        """Publish the marginal energy cost matrix as a Home Assistant sensor.

        The sensor state is the 1kWh/now cell (most immediate scenario).
        The full matrix is stored in the 'matrix' attribute.
        """
        if not getattr(self, "marginal_costs_matrix", None):
            return

        # State = most immediate cell: 1 extra kWh at the first (current) time window
        first_time_label = getattr(self, "marginal_time_labels", [""])[0]
        state_value = self.marginal_costs_matrix.get(1, {}).get(first_time_label, 0)

        attributes = {
            "friendly_name": "Marginal Energy Costs",
            "icon": "mdi:cash-plus",
            "state_class": "measurement",
            "unit_of_measurement": self.currency_symbols[1],
            "matrix": self.marginal_costs_matrix,
            "baseline_metric": dp2(getattr(self, "marginal_baseline_metric", 0)),
            "grid_import": getattr(self, "marginal_grid_import", {}),
            "grid_export": getattr(self, "marginal_grid_export", {}),
            "grid_import_now": dp2(self.rate_import.get(self.minutes_now, 0)),
            "grid_export_now": dp2(self.rate_export.get(self.minutes_now, 0)),
            "timestamp": self.now_utc.isoformat(),
        }

        min_import_cost = self.rate_min
        max_import_cost = self.rate_max

        cheap_threshold = min_import_cost * 1.2  # Example threshold for "cheap" energy
        moderate_threshold = max(max_import_cost * 0.5, min_import_cost * 1.5)  # "Moderate" if above cheap but below half of max

        for state_name in MARGINAL_EXTRA_KWH_LEVEL_NAMES:
            state_value = self.marginal_costs_matrix.get(MARGINAL_EXTRA_KWH_LEVELS[MARGINAL_EXTRA_KWH_LEVEL_NAMES.index(state_name)], {}).get(first_time_label, 0)
            is_cheap = state_value <= cheap_threshold
            is_moderate = not is_cheap and state_value <= moderate_threshold
            attributes["rate_now_{}_consumption".format(state_name)] = state_value

            self.dashboard_item(
                "binary_sensor.{}_marginal_rate_now_{}_is_cheap".format(self.prefix, state_name),
                state="on" if is_cheap else "off",
                attributes={"cost": state_value, "icon": "mdi:cash-check", "friendly_name": "{} consumption now is cheap".format(state_name.capitalize())},
            )
            self.dashboard_item(
                "binary_sensor.{}_marginal_rate_now_{}_is_moderate".format(self.prefix, state_name),
                state="on" if is_moderate else "off",
                attributes={"cost": state_value, "icon": "mdi:cash-alert", "friendly_name": "{} consumption now is moderate".format(state_name.capitalize())},
            )

        state_value = self.marginal_costs_matrix.get(1, {}).get(first_time_label, 0)
        self.dashboard_item(
            "sensor." + self.prefix + "_marginal_energy_costs",
            state=dp2(state_value),
            attributes=attributes,
        )
