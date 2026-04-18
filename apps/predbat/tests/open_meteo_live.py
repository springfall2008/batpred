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
Standalone live comparison of Open-Meteo vs Forecast.Solar solar forecasts.

Uses the real SolarAPI.download_open_meteo_data() and
SolarAPI.download_forecast_solar_data() from solcast.py — no duplication of
model logic or API call code.

Both sources use the same ARRAYS configuration so the results are directly
comparable. Hours are shown in local time (e.g. BST in summer).

Run from the repository root or coverage/ directory:
    source coverage/venv/bin/activate
    python3 apps/predbat/tests/open_meteo_live.py

This file is intentionally NOT registered in TEST_REGISTRY and is not
executed by run_all / run_cov.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta

import pytz

# ── Make apps/predbat importable regardless of working directory ───────────────
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PREDBAT_DIR = os.path.dirname(_TESTS_DIR)  # apps/predbat
if _PREDBAT_DIR not in sys.path:
    sys.path.insert(0, _PREDBAT_DIR)

from solcast import SolarAPI  # noqa: E402
from tests.test_solcast import MockBase  # noqa: E402

# ── Solar array definitions ────────────────────────────────────────────────────
# azimuth: Predbat convention — 0=North, ±180=South, -90=East, +90=West
# declination: angle from horizontal in degrees
ARRAYS = [
    {"postcode": "BS32 4SQ", "kwp": 1.56, "azimuth": -133, "declination": 23, "efficiency": 0.95},
    {"postcode": "BS32 4SQ", "kwp": 2.73, "azimuth":  45,  "declination": 45, "efficiency": 0.95},
]

LOCAL_TZ = pytz.timezone("Europe/London")
MAX_AGE_HOURS = 1.0  # re-fetch if cached data is older than this


# Stable cache directory alongside this script so data persists between runs
_CACHE_ROOT = os.path.join(_TESTS_DIR, ".om_live_cache")


def _make_solar_api(fs_api_key: str = None) -> tuple:
    """Create a real SolarAPI wired to a minimal mock base.  Returns (solar, base)."""
    # Use UTC midnight so FS and OM period_start timestamps share the same anchor.
    # FS uses (period_stamp - midnight_utc) to compute minute offsets; with UTC midnight
    # those offsets match the UTC timestamps that OM also produces, giving aligned output.
    now_utc = datetime.now(tz=pytz.utc)
    base = MockBase()
    base.config_root = _CACHE_ROOT  # stable dir so API responses are cached across runs
    base.now_utc = now_utc
    base.now_utc_exact = now_utc
    base.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    base.minutes_now = now_utc.hour * 60 + now_utc.minute
    # Use 60-minute plan intervals so forecast.solar per-slot kWh == kWh/hour
    base.plan_interval_minutes = 60
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

    fs_arrays = [{**a, "api_key": fs_api_key} if fs_api_key else a for a in ARRAYS]
    solar.initialize(
        solcast_host=None,
        solcast_api_key=None,
        solcast_sites=None,
        solcast_poll_hours=4,
        forecast_solar=fs_arrays,
        forecast_solar_max_age=MAX_AGE_HOURS,
        pv_forecast_today=None,
        pv_forecast_tomorrow=None,
        pv_forecast_d3=None,
        pv_forecast_d4=None,
        pv_scaling=1.0,
        open_meteo_forecast=ARRAYS,
        open_meteo_forecast_max_age=MAX_AGE_HOURS,
    )
    return solar, base


def _index_by_hour(sorted_data: list) -> dict:
    """Return dict mapping 'YYYY-MM-DDTHH' → (p50, p10) keyed by the first 13 chars of period_start."""
    result = {}
    for item in sorted_data:
        ts = item["period_start"]
        try:
            key = ts[:13]
        except (ValueError, TypeError):
            continue
        result[key] = (item.get("pv_estimate", 0.0), item.get("pv_estimate10", None))
    return result


def print_comparison_table(label: str, om_by_hour: dict, fs_by_hour: dict, day_str: str, tz_name: str) -> None:
    """Print a side-by-side comparison table for one day."""
    w = 70
    print()
    print("=" * w)
    print(f"  {label}  (hours in {tz_name})")
    print("=" * w)
    print(f"  {'Hour':>5}   {'OM P50 kW':>10}   {'OM P10 kW':>10}   {'FS P50 kW':>10}")
    print(f"  {'-'*5}   {'-'*10}   {'-'*10}   {'-'*10}")

    all_keys = sorted(set(list(om_by_hour.keys()) + list(fs_by_hour.keys())))
    day_keys = [k for k in all_keys if k[:10] == day_str]

    om_total = 0.0
    om10_total = 0.0
    fs_total = 0.0
    any_data = False

    for hour_key in day_keys:
        utc_dt = datetime.strptime(hour_key, "%Y-%m-%dT%H").replace(tzinfo=pytz.utc)
        hour_str = utc_dt.astimezone(LOCAL_TZ).strftime("%H:%M")
        om_p50, om_p10 = om_by_hour.get(hour_key, (0.0, None))
        fs_p50, _ = fs_by_hour.get(hour_key, (0.0, None))

        if om_p50 > 0.0 or fs_p50 > 0.0:
            any_data = True
            om10_str = f"{om_p10:10.3f}" if om_p10 is not None else "         -"
            print(f"  {hour_str:>5}   {om_p50:10.3f}   {om10_str}   {fs_p50:10.3f}")

        om_total += om_p50
        om10_total += (om_p10 if om_p10 is not None else 0.0)
        fs_total += fs_p50

    if not any_data:
        print("  (no data)")

    print(f"  {'-'*5}   {'-'*10}   {'-'*10}   {'-'*10}")
    print(f"  {'Total':>5}   {om_total:9.3f}kWh  {om10_total:9.3f}kWh  {fs_total:9.3f}kWh")
    print()


async def run(fs_api_key: str = None) -> None:
    """Fetch live data from both sources and print a side-by-side comparison."""
    now_local = datetime.now(tz=LOCAL_TZ)
    now_utc = datetime.now(tz=pytz.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    tomorrow_str = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
    tz_name = now_local.strftime("%Z")  # e.g. "BST" or "GMT" — for display only

    print()
    print(f"Solar forecast comparison  —  run date: {today_str} UTC  ({tz_name})")
    print()
    print(f"Arrays ({len(ARRAYS)} configured):")
    _tmp = SolarAPI.__new__(SolarAPI)
    for i, a in enumerate(ARRAYS, 1):
        az_api = SolarAPI.convert_azimuth(_tmp, a["azimuth"])
        print(f"  [{i}] postcode={a['postcode']}  kwp={a['kwp']}  declination={a['declination']}°  azimuth={a['azimuth']}° (→API: {az_api:.0f}°)  efficiency={a.get('efficiency', 1.0):.0%}")

    solar, base = _make_solar_api(fs_api_key=fs_api_key)
    print(f"  (cache: {_CACHE_ROOT}/cache/)")
    print("\nFetching Open-Meteo data (GTI forecast + ensemble P10)...")
    try:
        om_data, om_max_kwh = await solar.download_open_meteo_data()
        print(f"  {len(om_data)} data points  (combined max_kwh={om_max_kwh:.2f})")

        print("Fetching Forecast.Solar data (free tier, 2 days)...")
        fs_data, fs_max_kwh = await solar.download_forecast_solar_data()
        print(f"  {len(fs_data)} data points  (combined max_kwh={fs_max_kwh:.2f})")
    except Exception as e:
        print(f"  Error: {e}")
        raise

    # Both OM and FS now produce period_start in UTC (midnight_utc is UTC midnight).
    # Hours shown are UTC; with trapz integration OM "05:00 UTC" = energy during 05:00-06:00 UTC,
    # which aligns with FS "05:00 UTC" = energy accumulated for the hour ending at 06:00 UTC.
    # Sunrise ~05:10 UTC (06:10 BST) means the 05:00 UTC slot shows the first partial hour of sun.
    om_by_hour = _index_by_hour(om_data)
    fs_by_hour = _index_by_hour(fs_data)

    print_comparison_table(f"TODAY  ({today_str})", om_by_hour, fs_by_hour, today_str, tz_name)
    print_comparison_table(f"TOMORROW  ({tomorrow_str})", om_by_hour, fs_by_hour, tomorrow_str, tz_name)


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Live comparison of Open-Meteo vs Forecast.Solar")
    parser.add_argument("--fs-api-key", metavar="KEY", default=None, help="Forecast.Solar personal API key (enables professional account with more forecast days)")
    args = parser.parse_args()
    asyncio.run(run(fs_api_key=args.fs_api_key))


if __name__ == "__main__":
    main()

