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

# Extra cloud divergence applied to the PV10 scenario on top of the computed cloud factor, so the
# downside case diverges harder than the central one
CLOUD_FACTOR_PV10 = 0.2
# Window the envelope cloud model conserves energy over. Matches the half-hour rate slot so the
# modulation can never shift PV across a slot boundary, and gives six PREDICT_STEP buckets - enough
# granularity for the duty cycle to follow the p10/p50/p90 band's own asymmetry.
CLOUD_WINDOW_MINUTES = 30
# Ceiling for the p90 series' own modulation, as a multiple of DC array kWp. p90 is the top
# percentile the forecaster publishes, so its upside has no next percentile to reach for and is
# extrapolated instead; this rail stops a wide band extrapolating into a physically impossible
# array output. 1.2 allows for cloud-edge enhancement above nameplate.
CLOUD_ARRAY_MARGIN = 1.2
# Sentinel fetch_pv_forecast() returns when the forecast source declares no array size (Solcast and
# the HA integrations). Treated as "unknown", not as a real 9999 kWp array.
PV_ARRAY_KWP_UNKNOWN = 9999
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
# How far ahead a manual override may be placed. The two horizons differ on purpose: a manual
# charge/export/freeze/demand slot is bounded by the plan, since Predbat can only act on a slot the
# plan reaches, whereas a rate override is future tariff data that only has to be REMEMBERED until
# the plan reaches it - e.g. an energy supplier announcing a free-electricity hour several days out.
# 7 days is the ceiling the 'Day HH:MM' selection format can express anyway.
MANUAL_TIME_MAX_MINUTES = 48 * 60
MANUAL_RATE_MAX_MINUTES = 7 * 24 * 60

# 240v x 100 amps x 3 phases / 1000 to kW / 60 minutes in an hour is the maximum kWh in a 1 minute period
MAX_INCREMENT = 240 * 100 * 3 / 1000 / 60
MINUTE_WATT = 60 * 1000

# PV production (kWh) forecast across the remainder of a charge window above which low power charging is
# abandoned in favour of the max charge rate. Throttling the charge rate while the sun is shining stops the
# PV reaching the battery, the surplus is exported cheaply and the target is then made up with grid import,
# which increases the cost of the plan over the full rate charge the planner costed the window at.
LOW_POWER_PV_THRESHOLD = 0.1

# Fraction of the peak forecast PV power above which a plan_interval_minutes bucket is classed as
# "light" rather than "dark" when deciding where to split a charge window (calc_dawn). A charge window
# otherwise built from a single long cheap-rate period spanning sunrise would apply LOW_POWER_PV_THRESHOLD
# across the whole thing and abandon low power charging even for the still-dark hours before the sun is
# up (#4557) - splitting at dawn keeps the dark portion as its own window, genuinely PV-free, so it stays
# throttled. A fraction of that forecast's own peak, rather than a fixed Watts figure, scales with the
# site - a fixed threshold picked for a typical system would be noise-level for a large array and
# unreachable for a small one.
LOW_POWER_PV_LIGHT_FRACTION = 0.1

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

# Predbat's core charge/export status strings, ordered most-active-first. Used two ways: execute.py
# resolves one headline status across a multi-inverter fleet (#4446), and output.py breaks a tie when
# a history slot's dominant state is ambiguous - e.g. an even split between two states (#4843). Both
# need the same answer to "which of these two states is the more significant one to show".
CHARGE_STATE_PRECEDENCE = ["Charging", "Freeze charging", "Hold charging"]
EXPORT_STATE_PRECEDENCE = ["Exporting", "Freeze exporting", "Hold exporting"]
