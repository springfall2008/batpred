"""Data for the modern battery rate curves chart."""

from datetime import datetime

from battery_curve import charge_day
from battery_rate_grid import rate_grid_curve, rate_grid_table
from const import BATTERY_RATE_GRID_MAX, BATTERY_RATE_GRID_TEMP_MAX, BATTERY_RATE_GRID_TEMP_MIN, BATTERY_RATE_GRID_UNKNOWN_TEMP


def curves_chart_data(base, prefix, now_utc):
    """Return the active grids, their status and recent evidence for the chart."""
    today = datetime.strptime(charge_day(now_utc, 0, getattr(base, "local_tz", None)), "%Y-%m-%d").date()
    samples = getattr(base, "battery_rate_grid_samples", {}) or {}
    learned = getattr(base, "battery_rate_grid_learned", {}) or {}
    dashboard = getattr(base, "dashboard_values", {}) or {}
    result = []
    for kind, grid in zip(("charge", "discharge"), base.rate_grids()):
        evidence = []
        for day, entries in (samples.get(kind) or {}).items():
            try:
                age = (today - datetime.strptime(day, "%Y-%m-%d").date()).days
            except (TypeError, ValueError):
                continue
            if age <= 90:
                evidence.extend(entries)
        temperatures = [entry[1] for entry in evidence if entry[1] is not None]
        temperatures += (learned.get(kind) or {}).get("temp_range") or []
        low = max(int(min(temperatures)) - 5, BATTERY_RATE_GRID_TEMP_MIN) if temperatures else -10
        high = min(int(max(temperatures)) + 5, BATTERY_RATE_GRID_TEMP_MAX) if temperatures else 30
        degrees = list(range(low, high + 1))
        item = dashboard.get("binary_sensor.{}_battery_{}_rate_grid".format(prefix, kind), {})
        result.append(
            {
                "kind": kind,
                "state": item.get("state"),
                "attributes": item.get("attributes", {}) or {},
                "soc": list(range(101)),
                "temperature": degrees,
                "z": [[round(value, 3) for value in rate_grid_curve(grid, degree if degree != 0 else 0.5)[:101]] for degree in degrees],
                "evidence": [[entry[0], entry[1] if entry[1] is not None else BATTERY_RATE_GRID_UNKNOWN_TEMP, entry[2]] for entry in evidence],
                "table": rate_grid_table(grid),
            }
        )
    return {"chart": "curves", "ready": True, "max_rate": BATTERY_RATE_GRID_MAX, "grids": result}
