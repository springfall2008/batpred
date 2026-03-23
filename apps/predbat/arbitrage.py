# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=line-too-long

"""Arbitrage engine for target-driven grid import/export optimisation.

Analyses Agile import/export rates alongside solar and load forecasts to select
the minimum set of 30-minute charge/export slot pairs needed to hit a
user-defined daily profit target. Outputs pre-committed slot constraints
for injection into plan.py.
"""

from __future__ import annotations

SLOT_MINUTES = 30  # Agile slot size in minutes


class ArbitrageEngine:
    """Compute arbitrage schedules for dynamic tariffs."""

    def __init__(
        self,
        rate_import: dict,       # {minute: pence_per_kwh}
        rate_export: dict,       # {minute: pence_per_kwh}
        solar_forecast: dict,    # {minute: kW}
        load_forecast: dict,     # {minute: kW}
        battery_soc_percent: float,        # current SoC 0-100
        battery_capacity_kwh: float,       # usable capacity in kWh
        charge_rate_kw: float,             # max charge rate in kW
        discharge_rate_kw: float,          # max discharge rate in kW
        battery_efficiency: float,         # round-trip efficiency 0.0-1.0
        profit_target_daily: float,        # target daily profit in £
        arbitrage_reserve_percent: float,  # % of battery ring-fenced for arbitrage
        minutes_now: int,                  # current minute of day (0-1439)
    ):
        self.rate_import = rate_import
        self.rate_export = rate_export
        self.solar_forecast = solar_forecast
        self.load_forecast = load_forecast
        self.battery_soc_percent = battery_soc_percent
        self.battery_capacity_kwh = battery_capacity_kwh
        self.charge_rate_kw = charge_rate_kw
        self.discharge_rate_kw = discharge_rate_kw
        self.battery_efficiency = battery_efficiency
        self.profit_target_daily = profit_target_daily
        self.arbitrage_reserve_percent = arbitrage_reserve_percent
        self.minutes_now = minutes_now

    def score_slots(self) -> list[dict]:
        """Return scored list of charge/export slot pairs sorted by net profit.

        Each entry: {"charge_minute": int, "export_minute": int,
                     "net_profit_gbp": float, "charge_kwh": float,
                     "discharge_kwh": float}
        Only positive-spread pairs after efficiency losses are included.
        Confidence discount applied: slots further ahead score proportionally lower.
        Sorted descending by discounted net profit.
        """
        raise NotImplementedError

    def schedule_to_target(self) -> list[dict]:
        """Select minimum non-overlapping slot pairs to hit profit_target_daily.

        Returns chronological list of slot dicts:
        {"start": int, "end": int, "type": "charge"|"export", "target_soc": float}.
        If target is unachievable, returns the best possible schedule without error.
        """
        raise NotImplementedError

    def plan_constraints(self) -> list[dict]:
        """Return slot constraints ready for injection into plan.py.

        Format matches charge_window/export_window entries used by plan.py:
        {"start": minute, "end": minute, "average": rate_p_per_kwh,
         "min": 0, "max": target_soc, "constraint_type": "charge"|"export"}
        """
        raise NotImplementedError

    def projected_gain(self) -> float:
        """Return projected arbitrage profit for today in £."""
        raise NotImplementedError

    def opportunity_score(self) -> int:
        """Return 0-100 score representing current arbitrage opportunity quality."""
        raise NotImplementedError
