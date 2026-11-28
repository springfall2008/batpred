"""Checks for the modern battery rate grid data."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from web_curve_data import curves_chart_data


class CurvesChartDataTests(unittest.TestCase):
    """Keep grid data, evidence and missing status usable by the frontend."""

    def test_grids_and_evidence(self):
        """Current evidence is included, stale evidence is omitted and absent status is retained."""
        base = SimpleNamespace(
            local_tz=timezone.utc,
            battery_rate_grid_samples={"charge": {"2026-11-15": [[40, None, 0.5]], "2026-01-01": [[50, 10, 0.4]]}},
            battery_rate_grid_learned={},
            dashboard_values={"binary_sensor.predbat_battery_charge_rate_grid": {"state": "on", "attributes": {"cells": 4}}},
            rate_grids=lambda: ("charge", "discharge"),
        )
        with patch("web_curve_data.rate_grid_curve", return_value=[1.0] * 101), patch("web_curve_data.rate_grid_table", return_value={"soc": [0], "temperature": None, "rows": [[1.0]]}):
            data = curves_chart_data(base, "predbat", datetime(2026, 11, 15, tzinfo=timezone.utc))
        self.assertEqual(data["chart"], "curves")
        self.assertEqual(data["grids"][0]["attributes"]["cells"], 4)
        self.assertEqual(len(data["grids"][0]["evidence"]), 1)
        self.assertIsNone(data["grids"][1]["state"])
        self.assertEqual(len(data["grids"][0]["z"][0]), 101)


if __name__ == "__main__":
    unittest.main()
