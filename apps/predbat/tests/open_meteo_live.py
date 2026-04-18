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
Standalone live comparison of PV forecast sources using the real Predbat pipeline.

Calls fetch_pv_forecast() for each configured source (Open-Meteo, Forecast.Solar,
Solcast) and reads back the detailedForecast from sensor.predbat_pv_today — exactly
what Predbat would see after running its full fetch + calibration pipeline.

Hours are shown in local time (e.g. BST in summer). Energy totals are in kWh/hour.

Run from the repository root or coverage/ directory:
    source coverage/venv/bin/activate
    python3 apps/predbat/tests/open_meteo_live.py [--fs-api-key KEY] [--solcast-api-key KEY]

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

from const import TIME_FORMAT  # noqa: E402
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

# Stable cache directory alongside this script so API responses persist between runs
_CACHE_ROOT = os.path.join(_TESTS_DIR, ".om_live_cache")


async def _fetch_via_pipeline(source_name: str, forecast_solar, open_meteo_forecast, solcast_api_key: str, solcast_host: str, plan_interval_minutes: int) -> tuple:
    """Run fetch_pv_forecast() for one source and return what Predbat would store.

    Creates a minimal MockBase + SolarAPI, calls fetch_pv_forecast(), then reads
    back the detailedForecast from sensor.predbat_pv_today and _pv_tomorrow.

    Returns (today_total_kwh, today_detail, tomorrow_total_kwh, tomorrow_detail).
    """
    now_utc = datetime.now(tz=pytz.utc)
    base = MockBase()
    base.config_root = _CACHE_ROOT  # stable dir so API responses are cached across runs
    base.now_utc = now_utc
    base.now_utc_exact = now_utc
    base.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    base.minutes_now = now_utc.hour * 60 + now_utc.minute
    base.plan_interval_minutes = plan_interval_minutes
    base.log = lambda msg: print(f"  [{source_name}] {msg}")

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
        solcast_host=solcast_host or "https://api.solcast.com.au",
        solcast_api_key=solcast_api_key,
        solcast_sites=None,
        solcast_poll_hours=4,
        forecast_solar=forecast_solar,
        forecast_solar_max_age=MAX_AGE_HOURS,
        pv_forecast_today=None,
        pv_forecast_tomorrow=None,
        pv_forecast_d3=None,
        pv_forecast_d4=None,
        pv_scaling=1.0,
        open_meteo_forecast=open_meteo_forecast,
        open_meteo_forecast_max_age=MAX_AGE_HOURS,
    )

    await solar.fetch_pv_forecast()

    today_item = base.dashboard_items.get(f"sensor.{base.prefix}_pv_today", {})
    tomorrow_item = base.dashboard_items.get(f"sensor.{base.prefix}_pv_tomorrow", {})

    today_total = today_item.get("state", 0.0) or 0.0
    today_detail = today_item.get("attributes", {}).get("detailedForecast", [])
    tomorrow_total = tomorrow_item.get("state", 0.0) or 0.0
    tomorrow_detail = tomorrow_item.get("attributes", {}).get("detailedForecast", [])

    return today_total, today_detail, tomorrow_total, tomorrow_detail


def _aggregate_to_hourly(detail_forecast: list) -> dict:
    """Aggregate detailedForecast entries (kW average per period) into hourly kWh.

    The detailedForecast from publish_pv_stats contains pv_estimate in kW (average
    power for the slot). To get kWh for the slot: kW * period_hours. Slots are then
    summed per UTC hour to give a comparable kWh/hour value across all sources.

    Returns {hour_key: (kwh_p50, kwh_p10)} where hour_key is 'YYYY-MM-DDTHH'.
    """
    if not detail_forecast:
        return {}

    # Detect the actual period from consecutive entries
    period = 60  # default
    if len(detail_forecast) >= 2:
        try:
            t0 = datetime.strptime(detail_forecast[0]["period_start"], TIME_FORMAT)
            t1 = datetime.strptime(detail_forecast[1]["period_start"], TIME_FORMAT)
            detected = int(abs((t1 - t0).total_seconds() / 60))
            if 5 <= detected <= 60:
                period = detected
        except (ValueError, TypeError, KeyError):
            pass

    period_hours = period / 60.0
    hourly: dict = {}
    for entry in detail_forecast:
        try:
            dt = datetime.strptime(entry["period_start"], TIME_FORMAT)
        except (ValueError, TypeError):
            continue
        hour_key = dt.strftime("%Y-%m-%dT%H")
        kw = entry.get("pv_estimate", 0.0) or 0.0
        kw10 = entry.get("pv_estimate10", kw) or kw
        existing = hourly.get(hour_key, (0.0, 0.0))
        hourly[hour_key] = (existing[0] + kw * period_hours, existing[1] + kw10 * period_hours)
    return hourly


def print_comparison_table(label: str, sources: dict, day_str: str, tz_name: str) -> None:
    """Print a side-by-side comparison table for one day.

    sources is an ordered dict of {source_name: hourly_dict} where each hourly_dict
    maps 'YYYY-MM-DDTHH' → (kwh_p50, kwh_p10) as returned by _aggregate_to_hourly().
    Hours are shown in local time (tz_name).
    """
    source_names = list(sources.keys())
    col_w = 12
    n_cols = len(source_names)
    w = 10 + n_cols * (col_w + 3)
    print()
    print("=" * w)
    print(f"  {label}  (hours in {tz_name})  [kWh/hour]")
    print("=" * w)
    header = f"  {'Hour':>5}  "
    divider = f"  {'-'*5}  "
    for name in source_names:
        header += f"  {(name + ' kWh/h'):>{col_w}}"
        divider += f"  {'-'*col_w}"
    print(header)
    print(divider)

    all_keys = sorted(set(k for hrs in sources.values() for k in hrs.keys()))
    day_keys = [k for k in all_keys if k[:10] == day_str]

    totals = {name: 0.0 for name in source_names}
    any_data = False

    for hour_key in day_keys:
        utc_dt = datetime.strptime(hour_key, "%Y-%m-%dT%H").replace(tzinfo=pytz.utc)
        hour_str = utc_dt.astimezone(LOCAL_TZ).strftime("%H:%M")
        row_vals = {name: sources[name].get(hour_key, (0.0, 0.0))[0] for name in source_names}
        if any(v > 0.0 for v in row_vals.values()):
            any_data = True
            row = f"  {hour_str:>5}  "
            for name in source_names:
                row += f"  {row_vals[name]:>{col_w}.3f}"
            print(row)
        for name in source_names:
            totals[name] += row_vals.get(name, 0.0)

    if not any_data:
        print("  (no data)")

    print(divider)
    total_row = f"  {'Total':>5}  "
    for name in source_names:
        total_row += f"  {totals[name]:>{col_w}.3f}"
    print(total_row + "  kWh")
    print()


async def run(fs_api_key: str = None, solcast_api_key: str = None, solcast_host: str = None) -> None:
    """Fetch live data via fetch_pv_forecast() for each source and print comparison.

    Runs the full Predbat PV fetch pipeline (download → minute_data → pv_calibration
    → publish_pv_stats) for each enabled source, then reads the detailedForecast that
    Predbat would store in sensor.predbat_pv_today.
    """
    now_local = datetime.now(tz=LOCAL_TZ)
    now_utc = datetime.now(tz=pytz.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    tomorrow_str = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
    tz_name = now_local.strftime("%Z")  # e.g. "BST" or "GMT" — for display only

    print()
    print(f"Predbat PV forecast pipeline comparison  —  {today_str} UTC  ({tz_name})")
    print()
    print(f"Arrays ({len(ARRAYS)} configured):")
    _tmp = SolarAPI.__new__(SolarAPI)
    for i, a in enumerate(ARRAYS, 1):
        az_api = SolarAPI.convert_azimuth(_tmp, a["azimuth"])
        print(f"  [{i}] postcode={a['postcode']}  kwp={a['kwp']}  declination={a['declination']}°  azimuth={a['azimuth']}° (→API: {az_api:.0f}°)  efficiency={a.get('efficiency', 1.0):.0%}")
    print(f"  cache: {_CACHE_ROOT}/cache/")

    # results maps source_name → {"today": hourly_dict, "tomorrow": hourly_dict}
    results: dict = {}

    # ── Open-Meteo ────────────────────────────────────────────────────────────
    # Uses plan_interval_minutes=60 so OM hourly data maps 1:1 to 60-min slots.
    print("\nFetching via fetch_pv_forecast() [Open-Meteo] ...")
    try:
        om_today_total, om_today_detail, om_tmrw_total, om_tmrw_detail = await _fetch_via_pipeline(
            "OM",
            forecast_solar=None,
            open_meteo_forecast=ARRAYS,
            solcast_api_key=None,
            solcast_host=None,
            plan_interval_minutes=60,
        )
        print(f"  Today: {om_today_total:.2f} kWh   Tomorrow: {om_tmrw_total:.2f} kWh")
        results["OM"] = {"today": _aggregate_to_hourly(om_today_detail), "tomorrow": _aggregate_to_hourly(om_tmrw_detail)}
    except Exception as e:
        print(f"  Error: {e}")
        results["OM"] = {"today": {}, "tomorrow": {}}

    # ── Forecast.Solar ────────────────────────────────────────────────────────
    # Uses plan_interval_minutes=30 to match FS native 30-min resolution.
    # Slots are aggregated to hourly kWh by _aggregate_to_hourly().
    fs_arrays = [{**a, "api_key": fs_api_key} if fs_api_key else a for a in ARRAYS]
    print(f"\nFetching via fetch_pv_forecast() [Forecast.Solar — {'personal' if fs_api_key else 'free tier'}] ...")
    try:
        fs_today_total, fs_today_detail, fs_tmrw_total, fs_tmrw_detail = await _fetch_via_pipeline(
            "FS",
            forecast_solar=fs_arrays,
            open_meteo_forecast=None,
            solcast_api_key=None,
            solcast_host=None,
            plan_interval_minutes=30,
        )
        print(f"  Today: {fs_today_total:.2f} kWh   Tomorrow: {fs_tmrw_total:.2f} kWh")
        results["FS"] = {"today": _aggregate_to_hourly(fs_today_detail), "tomorrow": _aggregate_to_hourly(fs_tmrw_detail)}
    except Exception as e:
        print(f"  Error: {e}")
        results["FS"] = {"today": {}, "tomorrow": {}}

    # ── Solcast ───────────────────────────────────────────────────────────────
    if solcast_api_key:
        print("\nFetching via fetch_pv_forecast() [Solcast] ...")
        try:
            sc_today_total, sc_today_detail, sc_tmrw_total, sc_tmrw_detail = await _fetch_via_pipeline(
                "SC",
                forecast_solar=None,
                open_meteo_forecast=None,
                solcast_api_key=solcast_api_key,
                solcast_host=solcast_host,
                plan_interval_minutes=30,
            )
            print(f"  Today: {sc_today_total:.2f} kWh   Tomorrow: {sc_tmrw_total:.2f} kWh")
            results["SC"] = {"today": _aggregate_to_hourly(sc_today_detail), "tomorrow": _aggregate_to_hourly(sc_tmrw_detail)}
        except Exception as e:
            print(f"  Error: {e}")
            results["SC"] = {"today": {}, "tomorrow": {}}

    source_today = {name: results[name]["today"] for name in results}
    source_tomorrow = {name: results[name]["tomorrow"] for name in results}
    print_comparison_table(f"TODAY  ({today_str})", source_today, today_str, tz_name)
    print_comparison_table(f"TOMORROW  ({tomorrow_str})", source_tomorrow, tomorrow_str, tz_name)


def main() -> None:
    """Entry point — parse CLI arguments and invoke run()."""
    parser = argparse.ArgumentParser(description="Predbat PV forecast pipeline comparison (OM vs FS vs Solcast)")
    parser.add_argument("--fs-api-key", metavar="KEY", default=None, help="Forecast.Solar personal API key (enables professional account with more forecast days)")
    parser.add_argument("--solcast-api-key", metavar="KEY", default=None, help="Solcast API key to also fetch and display Solcast forecasts")
    parser.add_argument("--solcast-host", metavar="URL", default="https://api.solcast.com.au", help="Solcast API host (default: https://api.solcast.com.au)")
    args = parser.parse_args()
    asyncio.run(run(fs_api_key=args.fs_api_key, solcast_api_key=args.solcast_api_key, solcast_host=args.solcast_host))


if __name__ == "__main__":
    main()
