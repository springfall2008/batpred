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
        scored = []
        slot_hours = SLOT_MINUTES / 60.0
        future_slots = [
            m for m in range(self.minutes_now, 1440, SLOT_MINUTES)
            if m in self.rate_import
        ]

        for charge_minute in future_slots:
            charge_kwh = self.charge_rate_kw * slot_hours
            import_cost_gbp = (self.rate_import[charge_minute] / 100.0) * charge_kwh

            for export_minute in future_slots:
                if export_minute <= charge_minute:
                    continue
                if export_minute not in self.rate_export:
                    continue

                discharge_kwh = charge_kwh * self.battery_efficiency
                export_revenue_gbp = (self.rate_export[export_minute] / 100.0) * discharge_kwh
                raw_profit_gbp = export_revenue_gbp - import_cost_gbp

                # Only score profitable pairs; apply confidence discount after sign check
                if raw_profit_gbp <= 0:
                    continue

                # Confidence discount: linear from 1.0 at 0h ahead to 0.5 at 48h
                hours_ahead = (charge_minute - self.minutes_now) / 60.0
                confidence = max(0.5, 1.0 - (hours_ahead / 96.0))
                net_profit_gbp = raw_profit_gbp * confidence

                scored.append({
                    "charge_minute": charge_minute,
                    "export_minute": export_minute,
                    "net_profit_gbp": round(net_profit_gbp, 4),
                    "charge_kwh": round(charge_kwh, 3),
                    "discharge_kwh": round(discharge_kwh, 3),
                })

        scored.sort(key=lambda x: x["net_profit_gbp"], reverse=True)
        return scored

    def schedule_to_target(self) -> list[dict]:
        """Select minimum non-overlapping slot pairs to hit profit_target_daily.

        Returns chronological list of slot dicts:
        {"start": int, "end": int, "type": "charge"|"export", "target_soc": float}.
        If target is unachievable, returns the best possible schedule without error.
        """
        scored = self.score_slots()
        if not scored:
            return []

        slot_hours = SLOT_MINUTES / 60.0
        arb_capacity_kwh = self.battery_capacity_kwh * (self.arbitrage_reserve_percent / 100.0)
        soc_kwh = self.battery_capacity_kwh * (self.battery_soc_percent / 100.0)
        # Headroom within the arbitrage-reserved portion of the battery.
        # The arb portion is the top N% of capacity ring-fenced for grid arbitrage.
        non_arb_kwh = self.battery_capacity_kwh - arb_capacity_kwh
        soc_in_arb_portion = max(0.0, soc_kwh - non_arb_kwh)
        available_charge_kwh = max(0.0, arb_capacity_kwh - soc_in_arb_portion)

        committed_minutes: set = set()
        schedule: list = []
        accumulated_profit = 0.0

        for pair in scored:
            if accumulated_profit >= self.profit_target_daily:
                break

            charge_min = pair["charge_minute"]
            export_min = pair["export_minute"]
            charge_kwh = min(pair["charge_kwh"], available_charge_kwh)
            if charge_kwh <= 0:
                continue

            charge_range = set(range(charge_min, charge_min + SLOT_MINUTES))
            export_range = set(range(export_min, export_min + SLOT_MINUTES))
            if charge_range & committed_minutes or export_range & committed_minutes:
                continue

            discharge_kwh = charge_kwh * self.battery_efficiency
            import_cost = (self.rate_import[charge_min] / 100.0) * charge_kwh
            export_revenue = (self.rate_export[export_min] / 100.0) * discharge_kwh
            net_profit = export_revenue - import_cost

            soc_after_charge = min(
                100.0,
                ((soc_kwh + charge_kwh) / self.battery_capacity_kwh) * 100.0,
            )
            soc_after_discharge = max(
                0.0,
                soc_after_charge - (discharge_kwh / self.battery_capacity_kwh) * 100.0,
            )

            schedule.append({
                "start": charge_min,
                "end": charge_min + SLOT_MINUTES,
                "type": "charge",
                "target_soc": round(soc_after_charge, 1),
                "paired_export_minute": export_min,
            })
            schedule.append({
                "start": export_min,
                "end": export_min + SLOT_MINUTES,
                "type": "export",
                "target_soc": round(soc_after_discharge, 1),
                "paired_charge_minute": charge_min,
            })

            committed_minutes |= charge_range
            committed_minutes |= export_range
            accumulated_profit += net_profit
            available_charge_kwh -= charge_kwh

        schedule.sort(key=lambda x: x["start"])
        return schedule

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
