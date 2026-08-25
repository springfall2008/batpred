# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Global constants and default values used throughout PredBat.

Defines time formats, prediction intervals, retry limits, conversion factors,
mode options, and other constants shared across all modules.
"""

from datetime import datetime, timedelta

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_SOLCAST = "%Y-%m-%dT%H:%M:%S.%f0%z"  # 2024-05-31T18:00:00.0000000Z
TIME_FORMAT_OCTOPUS = "%Y-%m-%d %H:%M:%S%z"
TIME_FORMAT_SOLIS = "%Y-%m-%d %H:%M:%S"
PREDICT_STEP = 5
RUN_EVERY = 5
# Forecast scenarios simulated by the planner.
# PV_SCENARIO_PV10 must remain 1 so it stays interchangeable with the legacy pv10 boolean.
PV_SCENARIO_NOMINAL = 0
PV_SCENARIO_PV10 = 1
PV_SCENARIO_PV90 = 2
LOAD_FORECAST_HISTORY_MAX_DAYS = 30  # Max days of history used by the weighted-bucket load forecast (days_previous_auto)
CONFIG_ROOTS = ["/config", "/conf", "/homeassistant", "./"]
TIME_FORMAT_HA = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_HA_TZ = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_DAILY = "%Y-%m-%d"
TIMEOUT = 60 * 5
CONFIG_REFRESH_PERIOD = 60 * 8
INVERTER_MAX_RETRY = 10  # Maximum number of retries for inverter commands
INVERTER_MAX_RETRY_REST = 5  # Maximum number of retries for inverter REST commands
INVERTER_REST_TIMEOUT = 10  # Seconds to wait for a REST response before giving up (local network call, should be fast)
INVERTER_QUICK_UPDATE_SECONDS = 120  # Minimum seconds between quick inverter data updates
PREDBAT_MAX_CARS = 8  # Matches PK_MAX_CARS in prediction_kernel.cpp and the car_charging_rate/_1../_7 config items - the hard ceiling on num_cars
DEBUG_ENABLE_MAX_HOURS = 2  # Auto-disable switch.predbat_debug_enable after this long left on, to bound the raw per-cycle debug.yaml disk writes it triggers (and the C++ kernel bypass it forces) if left on by accident - the rotating debug-history buffer covers longer-term history at a coarser interval instead

# 240v x 100 amps x 3 phases / 1000 to kW / 60 minutes in an hour is the maximum kWh in a 1 minute period
MAX_INCREMENT = 240 * 100 * 3 / 1000 / 60
MINUTE_WATT = 60 * 1000

INVERTER_TEST = False  # Run inverter control self test

# Sentinel values for an export window's target SoC/limit (export_limits_best and friends).
# A real target is any value below EXPORT_LIMIT_FREEZE, expressed as a percentage 0-100
# (see calc_percent_limit) with the fractional part sometimes encoding a low-power export rate.
# prediction_kernel.cpp hardcodes the same two literals independently (it can't import this
# file) - unlike PREDBAT_MAX_CARS/PK_MAX_CARS above, they aren't yet named there too, so a value
# change here needs the matching literals found and updated by hand, in lockstep with a parity
# revision bump and a rebuild of all platform binaries.
EXPORT_LIMIT_FREEZE = 99.0  # Hold SoC, export only genuine PV surplus - no forced discharge
EXPORT_LIMIT_IDLE = 100.0  # Export window disabled entirely

# Create an array of times in the day in 5-minute intervals
BASE_TIME = datetime.strptime("00:00:00", "%H:%M:%S")
OPTIONS_TIME = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M:%S")) for minute in range(0, 24 * 60, 5)]

# Inverter modes
PREDBAT_MODE_OPTIONS = ["Monitor", "Control SOC only", "Control charge", "Control charge & discharge"]
PREDBAT_MODE_MONITOR = 0
PREDBAT_MODE_CONTROL_SOC = 1
PREDBAT_MODE_CONTROL_CHARGE = 2
PREDBAT_MODE_CONTROL_CHARGEDISCHARGE = 3
