#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
Standalone live test for Open-Meteo solar forecasting.

Uses the real SolarAPI.download_open_meteo_data() from solcast.py — no
duplication of the PVWatts model, azimuth conversion, or API call logic.

Configuration mirrors the forecast_solar entries in apps.yaml for BS16 9BJ.

Run from the repository root or any directory:
    source coverage/venv/bin/activate
    python3 apps/predbat/tests/open_meteo_live_test.py

This file is intentionally NOT registered in TEST_REGISTRY and is not
executed by run_all / run_cov.
"""

import asyncio
import os
import shutil
import sys
from datetime import date, datetime, timedelta

import pytz

# ── Make apps/predbat importable regardless of working directory ───────────────
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PREDBAT_DIR = os.path.dirname(_TESTS_DIR)  # apps/predbat
if _PREDBAT_DIR not in sys.path:
    sys.path.insert(0, _PREDBAT_DIR)

from solcast import SolarAPI  # noqa: E402
from tests.test_solcast import MockBase  # noqa: E402

# ── Solar array definitions (matching forecast_solar entries in apps.yaml) ─────
# azimuth: Predbat/Solcast convention — 0=North, ±180=South, -90=East, +90=West
# declination: angle from horizontal in degrees
ARRAYS = [
    {"postcode": "BS32 4SQ", "kwp": 1.56, "azimuth": -133, "declination": 23, "efficiency": 0.95},
    {"postcode": "BS32 4SQ", "kwp": 2.73, "azimuth":  45,  "declination": 45, "efficiency": 0.95},
]

OPEN_METEO_MAX_AGE_HOURS = 1.0  # re-fetch if cached data older than this


def _make_solar_api() -> tuple:
    """Create a real SolarAPI wired to a minimal mock base.  Returns (solar, base)."""
    now = datetime.now(tz=pytz.utc)
    base = MockBase()
    base.now_utc = now
    base.now_utc_exact = now
    base.midnight_utc = now.replace(hour=0, minute=0, second=0, microsecond=0)
    base.minutes_now = now.hour * 60 + now.minute
    # Print all log messages so errors from cache_get_url are visible
    base.log = lambda msg: print(f"  [log] {msg}")

    solar = SolarAPI.__new__(SolarAPI)
    solar.base = base
    solar.log = base.log
    solar.local_tz = base.local_tz
    solar.prefix = base.prefix
    solar.args = base.args
    solar.api_started = False
    solar.api_stop = False
    solar.last_success_timestamp = None
    solar.count_errors = 0

    solar.initialize(
        solcast_host=None,
        solcast_api_key=None,
        solcast_sites=None,
        solcast_poll_hours=4,
        forecast_solar=None,
        forecast_solar_max_age=4,
        pv_forecast_today=None,
        pv_forecast_tomorrow=None,
        pv_forecast_d3=None,
        pv_forecast_d4=None,
        pv_scaling=1.0,
        open_meteo_forecast=ARRAYS,
        open_meteo_forecast_max_age=OPEN_METEO_MAX_AGE_HOURS,
    )
    return solar, base


def print_day_table(label: str, rows: list) -> None:
    """Print a formatted table of hourly P50/P10 power values plus daily energy totals."""
    width = 58
    print()
    print("=" * width)
    print(f"  {label}")
    print("=" * width)

    if not rows:
        print("  (no data)")
        return

    print(f"  {'Hour':>5}   {'P50  kW':>9}   {'P10  kW':>9}")
    print(f"  {'-' * 5}   {'-' * 9}   {'-' * 9}")

    total_p50 = 0.0
    total_p10 = 0.0
    for ts, p50, p10 in rows:
        hour = ts[11:16]  # "HH:MM" from "YYYY-MM-DDTHH:MM:SS+0000"
        total_p50 += p50
        total_p10 += p10
        if p50 > 0.0 or p10 > 0.0:
            print(f"  {hour:>5}   {p50:>9.3f}   {p10:>9.3f}")

    print(f"  {'-' * 5}   {'-' * 9}   {'-' * 9}")
    # Each hourly value is in kW; one point per hour so sum == kWh
    print(f"  {'Total':>5}   {total_p50:>8.3f}kWh  {total_p10:>8.3f}kWh")
    print()


async def run() -> None:
    """Fetch live data from Open-Meteo and print today/tomorrow tables."""
    today_str = date.today().isoformat()
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()

    print()
    print(f"Open-Meteo solar forecast  —  run date: {today_str}")
    print()
    print(f"Arrays ({len(ARRAYS)} configured):")
    # Use SolarAPI.convert_azimuth to show the converted value consistently
    _tmp = SolarAPI.__new__(SolarAPI)
    for i, a in enumerate(ARRAYS, 1):
        az_om = SolarAPI.convert_azimuth(_tmp, a["azimuth"])
        print(f"  [{i}] postcode={a['postcode']}  kwp={a['kwp']}  declination={a['declination']}°  azimuth={a['azimuth']}° (→OM: {az_om:.0f}°)  efficiency={a.get('efficiency', 1.0):.0%}")

    print("\nFetching Open-Meteo data (forecast + ensemble)...")
    solar, base = _make_solar_api()
    try:
        sorted_data, max_kwh = await solar.download_open_meteo_data()
    finally:
        shutil.rmtree(base.config_root, ignore_errors=True)

    print(f"Received {len(sorted_data)} data points  (combined max_kwh={max_kwh:.2f})")

    today_rows = []
    tomorrow_rows = []
    for item in sorted_data:
        ts = item["period_start"]  # "YYYY-MM-DDTHH:MM:SS+0000"
        p50 = item["pv_estimate"]
        p10 = item["pv_estimate10"]
        day = ts[:10]
        if day == today_str:
            today_rows.append((ts, p50, p10))
        elif day == tomorrow_str:
            tomorrow_rows.append((ts, p50, p10))

    print_day_table(f"TODAY  ({today_str})  — combined across all arrays", today_rows)
    print_day_table(f"TOMORROW  ({tomorrow_str})  — combined across all arrays", tomorrow_rows)


def main() -> None:
    """Entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
