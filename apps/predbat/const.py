# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from datetime import datetime, timedelta

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_SOLCAST = "%Y-%m-%dT%H:%M:%S.%f0%z"  # 2024-05-31T18:00:00.0000000Z
TIME_FORMAT_OCTOPUS = "%Y-%m-%d %H:%M:%S%z"
TIME_FORMAT_SOLIS = "%Y-%m-%d %H:%M:%S"
PREDICT_STEP = 5
RUN_EVERY = 5
CONFIG_ROOTS = ["/config", "/conf", "/homeassistant", "./"]
TIME_FORMAT_HA = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_HA_TZ = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_DAILY = "%Y-%m-%d"
TIMEOUT = 60 * 5
CONFIG_REFRESH_PERIOD = 60 * 8
INVERTER_MAX_RETRY = 10  # Maximum number of retries for inverter commands
INVERTER_MAX_RETRY_REST = 5  # Maximum number of retries for inverter REST commands
INVERTER_QUICK_UPDATE_SECONDS = 60  # Minimum seconds between quick inverter data updates

# 240v x 100 amps x 3 phases / 1000 to kW / 60 minutes in an hour is the maximum kWh in a 1 minute period
MAX_INCREMENT = 240 * 100 * 3 / 1000 / 60
MINUTE_WATT = 60 * 1000

INVERTER_TEST = False  # Run inverter control self test

# Create an array of times in the day in 5-minute intervals
BASE_TIME = datetime.strptime("00:00:00", "%H:%M:%S")
OPTIONS_TIME = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M:%S")) for minute in range(0, 24 * 60, 5)]

# Inverter modes
PREDBAT_MODE_OPTIONS = ["Monitor", "Control SOC only", "Control charge", "Control charge & discharge"]
PREDBAT_MODE_MONITOR = 0
PREDBAT_MODE_CONTROL_SOC = 1
PREDBAT_MODE_CONTROL_CHARGE = 2
PREDBAT_MODE_CONTROL_CHARGEDISCHARGE = 3
