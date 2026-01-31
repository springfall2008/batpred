# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Fox API Library
# -----------------------------------------------------------------------------

import asyncio
from datetime import datetime, timedelta, timezone
import time
import hashlib
import aiohttp
import json
import argparse
import random
from component_base import ComponentBase

# Define TIME_FORMAT_HA locally to avoid dependency issues
TIME_FORMAT_HA = "%Y-%m-%dT%H:%M:%S%z"

BASE_TIME = datetime.strptime("00:00", "%H:%M")
OPTIONS_TIME = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M")) for minute in range(0, 24 * 60, 1)]
OPTIONS_TIME_FULL = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M") + ":00") for minute in range(0, 24 * 60, 1)]

FOX_DOMAIN = "https://www.foxesscloud.com"
FOX_LANG = "en"
TIMEOUT = 60
FOX_RETRIES = 10
FOX_SETTINGS = ["ExportLimit", "MaxSoc", "GridCode", "WorkMode", "ExportLimitPower", "MinSoc", "MinSocOnGrid"]
OPTIONS_WORK_MODE = ["SelfUse", "ForceCharge", "ForceDischarge", "Feedin"]

# Dummy attribute table for testing
fox_attribute_table = {"mode": {}}


def schedules_are_equal(time_now, schedule1, schedule2):
    """
    Docstring for schedules_are_equal

    :param time_now: Datetime object
    :param schedule1: Schedule to compare
    :param schedule2: Schedule to compare
    """
    schedule1 = sort_schedule_by_start_time(schedule1)
    schedule2 = sort_schedule_by_start_time(schedule2)

    same = True
    if len(schedule1) != len(schedule2):
        same = False
    else:
        for i in range(0, len(schedule2)):
            for key in schedule2[i]:
                if schedule2[i][key] != schedule1[i].get(key, None):
                    same = False
                    break
            if not same:
                break
    return same


def end_minute_inclusive_to_exclusive(end_hour, end_minute):
    """
    Adjust end minute that is inclusive to exclusive (add 1 minute).
    Handles overflow to next hour and special case at end of day.

    Args:
        end_hour: Hour value (0-23)
        end_minute: Minute value (0-59)

    Returns:
        Tuple of (adjusted_end_hour, adjusted_end_minute)
    """
    if end_minute != 0:
        end_minute += 1
        if end_minute == 60:
            if end_hour == 23:
                end_minute = 59
            else:
                end_minute = 0
                end_hour += 1
    return end_hour, end_minute


def end_minute_exclusive_to_inclusive(end_hour, end_minute):
    """
    Convert exclusive end time to inclusive format (subtract 1 minute).
    Handles underflow to previous hour and special case at start of day.

    Args:
        end_hour: Hour value (0-23)
        end_minute: Minute value (0-59)

    Returns:
        Tuple of (adjusted_end_hour, adjusted_end_minute)
    """
    if end_minute == 0:
        end_hour -= 1
        end_minute = 59
        if end_hour < 0:
            end_hour = 23
            end_minute = 59
    elif end_minute != 59:
        end_minute -= 1
    return end_hour, end_minute


def minutes_to_schedule_time(start_hour, start_minute):
    start_minutes = start_hour * 60 + start_minute
    return start_minutes


def schedule_strip_disabled(schedule):
    new_schedule = []
    for entry in schedule:
        if entry.get("enable", 0) == 1:
            new_schedule.append(entry)
    return new_schedule


def sort_schedule_by_start_time(schedule):
    schedule = schedule_strip_disabled(schedule)
    schedule = sorted(schedule, key=lambda x: (minutes_to_schedule_time(x["startHour"], x["startMinute"])))
    return schedule


def validate_schedule(new_schedule, reserve, fdPwr_max):
    # Sort schedule by start time, closest to midnight first
    new_schedule = sort_schedule_by_start_time(new_schedule)
    if not new_schedule:
        # No schedule entries so disable
        new_schedule = [{"enable": 1, "startHour": 0, "startMinute": 0, "endHour": 23, "endMinute": 59, "workMode": "SelfUse", "fdSoc": reserve, "maxSoc": 100, "fdPwr": fdPwr_max, "minSocOnGrid": reserve}]
        return new_schedule

    # Process all schedule entries
    result_schedule = []

    # Adjust end times to be inclusive for all entries
    for entry in new_schedule:
        end_hour = entry["endHour"]
        end_minute = entry["endMinute"]
        end_hour, end_minute = end_minute_exclusive_to_inclusive(end_hour, end_minute)
        entry["endHour"] = end_hour
        entry["endMinute"] = end_minute

    # Add demand mode before first entry if needed
    first_entry = new_schedule[0]
    if first_entry["startHour"] != 0 or first_entry["startMinute"] != 0:
        demand_end_hour, demand_end_minute = end_minute_exclusive_to_inclusive(first_entry["startHour"], first_entry["startMinute"])
        result_schedule.append({"enable": 1, "startHour": 0, "startMinute": 0, "endHour": demand_end_hour, "endMinute": demand_end_minute, "workMode": "SelfUse", "fdSoc": reserve, "maxSoc": 100, "fdPwr": fdPwr_max, "minSocOnGrid": reserve})

    # Add schedule entries and fill gaps between them
    for i, entry in enumerate(new_schedule):
        result_schedule.append(entry)

        # Check if there's a gap between this entry and the next
        if i < len(new_schedule) - 1:
            next_entry = new_schedule[i + 1]
            current_end_hour = entry["endHour"]
            current_end_minute = entry["endMinute"]
            next_start_hour = next_entry["startHour"]
            next_start_minute = next_entry["startMinute"]

            # Calculate gap start time (one minute after current entry ends)
            gap_start_hour = current_end_hour
            gap_start_minute = current_end_minute
            if gap_start_minute == 59:
                gap_start_hour += 1
                gap_start_minute = 0
            else:
                gap_start_minute += 1

            # Check if there's actually a gap
            gap_start_minutes = gap_start_hour * 60 + gap_start_minute
            next_start_minutes = next_start_hour * 60 + next_start_minute

            if gap_start_minutes < next_start_minutes:
                # Fill the gap with SelfUse
                gap_end_hour, gap_end_minute = end_minute_exclusive_to_inclusive(next_start_hour, next_start_minute)
                result_schedule.append(
                    {"enable": 1, "startHour": gap_start_hour, "startMinute": gap_start_minute, "endHour": gap_end_hour, "endMinute": gap_end_minute, "workMode": "SelfUse", "fdSoc": reserve, "maxSoc": 100, "fdPwr": fdPwr_max, "minSocOnGrid": reserve}
                )

    # Add demand mode after last entry if needed
    last_entry = new_schedule[-1]
    if last_entry["endHour"] != 23 or last_entry["endMinute"] != 59:
        demand_start_hour = last_entry["endHour"]
        demand_start_minute = last_entry["endMinute"]
        if demand_start_minute == 59:
            demand_start_hour += 1
            demand_start_minute = 0
        else:
            demand_start_minute += 1
        result_schedule.append({"enable": 1, "startHour": demand_start_hour, "startMinute": demand_start_minute, "endHour": 23, "endMinute": 59, "workMode": "SelfUse", "fdSoc": reserve, "maxSoc": 100, "fdPwr": fdPwr_max, "minSocOnGrid": reserve})

    return result_schedule


class FoxAPI(ComponentBase):
    """Fox API client."""

    def initialize(self, key, automatic, inverter_sn=None):
        """Initialize the Fox API component"""
        self.key = key
        self.automatic = automatic
        self.failures_total = 0
        self.device_list = []
        self.device_detail = {}
        self.device_power_generation = {}
        self.available_variables = {}
        self.device_values = {}
        self.device_settings = {}
        self.device_production_month = {}
        self.device_production_year = {}
        self.device_battery_charging_time = {}
        self.device_scheduler = {}
        self.device_current_schedule = {}
        self.local_schedule = {}
        self.fdpwr_max = {}
        self.fdsoc_min = {}
        # Rate limiting tracking
        self.requests_today = 0
        self.rate_limit_errors_today = 0
        self.start_time_today = None
        self.last_midnight_utc = None

        # Convert inverter_sn to list
        if inverter_sn is None:
            self.inverter_sn_filter = []
        elif isinstance(inverter_sn, str):
            self.inverter_sn_filter = [inverter_sn]
        else:
            self.inverter_sn_filter = inverter_sn

    def should_allow_retry(self):
        """
        Calculate if retries should be allowed based on current API usage rate.
        Returns True if rate is <= 60/hour, False otherwise.
        Uses 30-minute minimum floor to prevent false positives during cold start.
        """
        if not self.start_time_today:
            return True

        elapsed_seconds = max((datetime.now(timezone.utc) - self.start_time_today).total_seconds(), 1800)
        hourly_rate = (self.requests_today * 3600) / elapsed_seconds
        return hourly_rate <= 60

    def is_alive(self):
        """
        Check if the API is alive
        """
        return self.api_started and self.device_list

    async def run(self, seconds, first):
        """
        Main run loop
        """
        # Initialize start time on first run
        if first:
            self.start_time_today = datetime.now(timezone.utc)
            self.last_midnight_utc = self.midnight_utc

        # Check for midnight boundary crossing and reset daily counters
        current_midnight = self.midnight_utc
        if self.last_midnight_utc is not None and self.last_midnight_utc != current_midnight:
            # Midnight has passed - reset daily counters
            self.log(f"Fox: Midnight reset - requests_today: {self.requests_today}, " f"rate_limit_errors_today: {self.rate_limit_errors_today}")
            self.requests_today = 0
            self.rate_limit_errors_today = 0
            self.start_time_today = datetime.now(timezone.utc)
            self.last_midnight_utc = current_midnight

        # Log API usage statistics
        if self.start_time_today:
            elapsed_seconds = max((datetime.now(timezone.utc) - self.start_time_today).total_seconds(), 1800)
            elapsed_minutes = elapsed_seconds / 60
            hourly_rate = (self.requests_today * 3600) / elapsed_seconds
            retry_allowed = self.should_allow_retry()
            self.log(f"Fox: API usage: {self.requests_today} requests over {elapsed_minutes:.1f} minutes " f"({hourly_rate:.1f}/hour), retry_allowed={retry_allowed}")

        if first:
            # Only do these once as battery charging times are ignored with the scheduler
            # and we get the realtime data every 5 minutes
            await self.get_device_list()
            self.log("Fox API: Found {} devices".format(len(self.device_list)))
            if not self.device_list:
                self.log("Error: FoxAPI: No devices found, unable to start API")
                return False

            # Get per device data
            for device in self.device_list:
                sn = device.get("deviceSN", None)
                if sn:
                    await self.get_device_detail(sn)
                    await self.get_device_history(sn)
                    await self.get_battery_charging_time(sn)

        if first or (seconds % (60 * 60) == 0):
            # Regular updates for registers and scheduler data
            for device in self.device_list:
                sn = device.get("deviceSN", None)
                if sn:
                    await self.get_device_settings(sn)
                    await self.get_schedule_settings_ha(sn)
                    await self.get_scheduler(sn)
                    await self.compute_schedule(sn)

        # Refresh total metrics every 15 minutes
        if first or (seconds % (15 * 60) == 0):
            for device in self.device_list:
                sn = device.get("deviceSN", None)
                if sn:
                    await self.get_device_production_month(sn)

        # Real time data every 5 minutes
        if first or (seconds % (5 * 60) == 0):
            for device in self.device_list:
                sn = device.get("deviceSN", None)
                if sn:
                    await self.get_real_time_data(sn)
            # Refresh HA entities
            await self.publish_data()

        # Automatic configuration on first run
        if first and self.automatic:
            await self.automatic_config()

        return True

    async def get_available_variables(self):
        """
        Get available variables for the device

        {
            'todayYield': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'Today’s power generation', 'Energy-storage inverter': True},
            'pvPower': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'PVPower', 'Energy-storage inverter': True},
            'pv1Volt': {'unit': 'V', 'Grid-tied inverter': True, 'name': 'PV1Volt', 'Energy-storage inverter': True},
            'pv1Current': {'unit': 'A', 'Grid-tied inverter': True, 'name': 'PV1Current', 'Energy-storage inverter': True},
            'pv1Power': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'PV1Power', 'Energy-storage inverter': True},
            'pv2Volt': {'unit': 'V', 'Grid-tied inverter': True, 'name': 'PV2Volt', 'Energy-storage inverter': True},
            'pv2Current': {'unit': 'A', 'Grid-tied inverter': True, 'name': 'PV2Current', 'Energy-storage inverter': True},
            'pv2Power': {'unit': 'kW', 'name': 'PV2Power'},
            ..
            'epsPower': {'unit': 'kW', 'Grid-tied inverter': False, 'name': 'EPSPower', 'Energy-storage inverter': True},
            'epsCurrentR': {'unit': 'A', 'Grid-tied inverter': False, 'name': 'EPS-RCurrent', 'Energy-storage inverter': True},
            'epsVoltR': {'unit': 'V', 'Grid-tied inverter': False, 'name': 'EPS-RVolt', 'Energy-storage inverter': True},
            'epsPowerR': {'unit': 'kW', 'Grid-tied inverter': False, 'name': 'EPS-RPower', 'Energy-storage inverter': True},
            'epsCurrentS': {'unit': 'A', 'Grid-tied inverter': False, 'name': 'EPS-SCurrent', 'Energy-storage inverter': True},
            'epsVoltS': {'unit': 'V', 'Grid-tied inverter': False, 'name': 'EPS-SVolt', 'Energy-storage inverter': True},
            'epsPowerS': {'unit': 'kW', 'Grid-tied inverter': False, 'name': 'EPS-SPower', 'Energy-storage inverter': True},
            'epsCurrentT': {'unit': 'A', 'Grid-tied inverter': False, 'name': 'EPS-TCurrent', 'Energy-storage inverter': True},
            'epsVoltT': {'unit': 'V', 'Grid-tied inverter': False, 'name': 'EPS-TVolt', 'Energy-storage inverter': True},
            'epsPowerT': {'unit': 'kW', 'Grid-tied inverter': False, 'name': 'EPS-TPower', 'Energy-storage inverter': True},
            'RCurrent': {'unit': 'A', 'Grid-tied inverter': True, 'name': 'RCurrent', 'Energy-storage inverter': True},
            'RVolt': {'unit': 'V', 'Grid-tied inverter': True, 'name': 'RVolt', 'Energy-storage inverter': True},
            'RFreq': {'unit': 'Hz', 'Grid-tied inverter': True, 'name': 'RFreq', 'Energy-storage inverter': True},
            'RPower': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'RPower', 'Energy-storage inverter': True},
            'SCurrent': {'unit': 'A', 'Grid-tied inverter': True, 'name': 'SCurrent', 'Energy-storage inverter': True},
            'SVolt': {'unit': 'V', 'Grid-tied inverter': True, 'name': 'SVolt', 'Energy-storage inverter': True},
            'SFreq': {'unit': 'Hz', 'Grid-tied inverter': True, 'name': 'SFreq', 'Energy-storage inverter': True},
            'SPower': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'SPower', 'Energy-storage inverter': True},
            'TCurrent': {'unit': 'A', 'Grid-tied inverter': True, 'name': 'TCurrent', 'Energy-storage inverter': True},
            'TVolt': {'unit': 'V', 'Grid-tied inverter': True, 'name': 'TVolt', 'Energy-storage inverter': True},
            'TFreq': {'unit': 'Hz', 'Grid-tied inverter': True, 'name': 'TFreq', 'Energy-storage inverter': True},
            'TPower': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'TPower', 'Energy-storage inverter': True},
            'ambientTemperation': {'unit': '℃', 'Grid-tied inverter': True, 'name': 'AmbientTemperature', 'Energy-storage inverter': True},
            'boostTemperation': {'unit': '℃', 'Grid-tied inverter': True, 'name': 'BoostTemperature', 'Energy-storage inverter': True},
            'invTemperation': {'unit': '℃', 'Grid-tied inverter': True, 'name': 'InvTemperation', 'Energy-storage inverter': True},
            'chargeTemperature': {'unit': '℃', 'Grid-tied inverter': True, 'name': 'ChargeTemperature', 'Energy-storage inverter': True},
            'batTemperature': {'unit': '℃', 'Grid-tied inverter': False, 'name': 'batTemperature', 'Energy-storage inverter': True},
            'dspTemperature': {'unit': '℃', 'Grid-tied inverter': True, 'name': 'DSPTemperature', 'Energy-storage inverter': True},
            'loadsPower': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'Load Power', 'Energy-storage inverter': True},
            'loadsPowerR': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'LoadsRPower', 'Energy-storage inverter': True},
            'loadsPowerS': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'LoadsSPower', 'Energy-storage inverter': True},
            'loadsPowerT': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'LoadsTPower', 'Energy-storage inverter': True},
            'generationPower': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'Output Power', 'Energy-storage inverter': True},
            'feedinPower': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'Feed-in Power', 'Energy-storage inverter': True},
            'gridConsumptionPower': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'GridConsumption Power', 'Energy-storage inverter': True},
            'invBatVolt': {'unit': 'V', 'Grid-tied inverter': True, 'name': 'InvBatVolt', 'Energy-storage inverter': True},
            'invBatCurrent': {'note': 'Positive Discharge, Negative Charge', 'unit': 'A', 'Grid-tied inverter': False, 'name': 'InvBatCurrent', 'Energy-storage inverter': True},
            'invBatPower': {'note': 'Positive Discharge, Negative Charge', 'unit': 'kW', 'Grid-tied inverter': False, 'name': 'invBatPower', 'Energy-storage inverter': True},
            'batChargePower': {'unit': 'kW', 'Grid-tied inverter': False, 'name': 'Charge Power', 'Energy-storage inverter': True},
            'batDischargePower': {'unit': 'kW', 'Grid-tied inverter': False, 'name': 'Discharge Power', 'Energy-storage inverter': True},
            'batVolt': {'unit': 'V', 'Grid-tied inverter': False, 'name': 'BatVolt', 'Energy-storage inverter': True},
            'batCurrent': {'unit': 'A', 'Grid-tied inverter': False, 'name': 'BatCurrent', 'Energy-storage inverter': True},
            'meterPower': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'MeterPower', 'Energy-storage inverter': True},
            'meterPower2': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'Meter2Power', 'Energy-storage inverter': True},
            'meterPowerR': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'MeterRPower', 'Energy-storage inverter': True},
            'meterPowerS': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'MeterSPower', 'Energy-storage inverter': True},
            'meterPowerT': {'unit': 'kW', 'Grid-tied inverter': True, 'name': 'MeterTPower', 'Energy-storage inverter': True},
            'SoC': {'unit': '%', 'Grid-tied inverter': False, 'name': 'SoC', 'Energy-storage inverter': True},
            'ReactivePower': {'unit': 'kVar', 'Grid-tied inverter': True, 'name': 'ReactivePower', 'Energy-storage inverter': True},
            'PowerFactor': {'Grid-tied inverter': True, 'name': 'PowerFactor', 'Energy-storage inverter': True},
            'generation': {'unit': 'kWh', 'Grid-tied inverter': True, 'name': 'Cumulative power generation', 'Energy-storage inverter': True},
            'ResidualEnergy': {'unit': '0.01kWh', 'Grid-tied inverter': False, 'name': 'Battery Residual Energy', 'Energy-storage inverter': True},
            'runningState': {'Grid-tied inverter': True, 'name': 'Running State', 'Energy-storage inverter': True, 'enum': {'165': 'fault', '166': 'permanent-fault', '167': 'standby', '168': 'upgrading', '169': 'fct', '170': 'illegal', '160': 'self-test', '161': 'waiting', '162': 'checking', '163': 'on-grid', '164': 'off-grid'}},
            'batStatus': {'Grid-tied inverter': False, 'name': 'Battery Status', 'Energy-storage inverter': True},
            'batStatusV2': {'Grid-tied inverter': False, 'name': 'Battery Status Name', 'Energy-storage inverter': True},
            'currentFault': {'Grid-tied inverter': True, 'name': 'The current error code is reported', 'Energy-storage inverter': True},
            'currentFaultCount': {'Grid-tied inverter': True, 'name': 'The number of errors', 'Energy-storage inverter': True},
            'energyThroughput': {'unit': 'Wh', 'Grid-tied inverter': False, 'name': 'Battery throughput', 'Energy-storage inverter': True},
            'SOH': {'unit': '%', 'Grid-tied inverter': False, 'name': 'SOH', 'Energy-storage inverter': True},
            'gridConsumption': {'unit': 'kWh', 'Grid-tied inverter': True, 'name': 'Total grid electricity consumption', 'Energy-storage inverter': True},
            'loads': {'unit': 'kWh', 'Grid-tied inverter': True, 'name': 'Load power consumption', 'Energy-storage inverter': True},
            'feedin': {'unit': 'kWh', 'Grid-tied inverter': True, 'name': 'The total energy of the feeder', 'Energy-storage inverter': True},
            'chargeEnergyToTal': {'unit': 'kWh', 'Grid-tied inverter': False, 'name': 'Total charge energy', 'Energy-storage inverter': True},
            'dischargeEnergyToTal': {'unit': 'kWh', 'Grid-tied inverter': False, 'name': 'Total discharge energy', 'Energy-storage inverter': True}

        """
        GET_AVAILABLE_VARIABLES = "/op/v0/device/variable/get"
        result = await self.request_get(GET_AVAILABLE_VARIABLES)
        available_data = {}
        if result is not None and isinstance(result, list):
            for variable_item in result:
                for variable_id in variable_item:
                    variable = variable_item.get(variable_id, {})
                    name = variable.get("name", {})
                    name = name.get(FOX_LANG, "")
                    variable["name"] = name
                    available_data[variable_id] = variable
            self.available_variables = available_data

    async def get_real_time_data(self, deviceSN):
        """
        Get real-time data
        [
            {'datas':
                [
                    {'unit': 'kW', 'name': 'PVPower', 'variable': 'pvPower', 'value': 1.8559999999999999},
                    {'unit': 'V', 'name': 'PV1Volt', 'variable': 'pv1Volt', 'value': 372.8},
                    {'unit': 'A', 'name': 'PV1Current', 'variable': 'pv1Current', 'value': 3.0},
                    {'unit': 'kW', 'name': 'PV1Power', 'variable': 'pv1Power', 'value': 1.128},
                    {'unit': 'V', 'name': 'PV2Volt', 'variable': 'pv2Volt', 'value': 236.4},
                    {'unit': 'A', 'name': 'PV2Current', 'variable': 'pv2Current', 'value': 3.0},
                    {'unit': 'kW', 'name': 'PV2Power', 'variable': 'pv2Power', 'value': 0.728},
                    {'unit': 'V', 'name': 'PV3Volt', 'variable': 'pv3Volt', 'value': 3.4},
                    {'unit': 'A', 'name': 'PV3Current', 'variable': 'pv3Current', 'value': 0.0},
                    {'unit': 'kW', 'name': 'PV3Power', 'variable': 'pv3Power', 'value': 0.0},
                    {'unit': 'V', 'name': 'PV4Volt', 'variable': 'pv4Volt', 'value': 0.0},
                    {'unit': 'A', 'name': 'PV4Current', 'variable': 'pv4Current', 'value': 0.0},
                    {'unit': 'kW', 'name': 'PV4Power', 'variable': 'pv4Power', 'value': 0.0},
                    {'unit': 'kW', 'name': 'EPSPower', 'variable': 'epsPower', 'value': 0.0},
                    {'unit': 'A', 'name': 'EPS-RCurrent', 'variable': 'epsCurrentR', 'value': 1.0},
                    {'unit': 'V', 'name': 'EPS-RVolt', 'variable': 'epsVoltR', 'value': 246.4},
                    {'unit': 'kW', 'name': 'EPS-RPower', 'variable': 'epsPowerR', 'value': 0.0},
                    {'unit': 'A', 'name': 'RCurrent', 'variable': 'RCurrent', 'value': 32.3},
                    {'unit': 'V', 'name': 'RVolt', 'variable': 'RVolt', 'value': 247.4},
                    {'unit': 'Hz', 'name': 'RFreq', 'variable': 'RFreq', 'value': 49.97},
                    {'unit': 'kW', 'name': 'RPower', 'variable': 'RPower', 'value': 7.993},
                    {'unit': '℃', 'name': 'AmbientTemperature', 'variable': 'ambientTemperation', 'value': 33.5},
                    {'unit': '℃', 'name': 'InvTemperation', 'variable': 'invTemperation', 'value': 29.7},
                    {'unit': '℃', 'name': 'batTemperature', 'variable': 'batTemperature', 'value': 33.6},
                    {'unit': 'kW', 'name': 'Load Power', 'variable': 'loadsPower', 'value': 17.046},
                    {'unit': 'kW', 'name': 'Output Power', 'variable': 'generationPower', 'value': 7.993},
                    {'unit': 'kW', 'name': 'Feed-in Power', 'variable': 'feedinPower', 'value': 0.0},
                    {'unit': 'kW', 'name': 'GridConsumption Power', 'variable': 'gridConsumptionPower', 'value': 9.053},
                    {'unit': 'V', 'name': 'InvBatVolt', 'variable': 'invBatVolt', 'value': 400.6},
                    {'unit': 'A', 'name': 'InvBatCurrent', 'variable': 'invBatCurrent', 'value': 16.4},
                    {'unit': 'kW', 'name': 'invBatPower', 'variable': 'invBatPower', 'value': 6.604},
                    {'unit': 'kW', 'name': 'Charge Power', 'variable': 'batChargePower', 'value': 0.0},
                    {'unit': 'kW', 'name': 'Discharge Power', 'variable': 'batDischargePower', 'value': 6.604},
                    {'unit': 'V', 'name': 'BatVolt', 'variable': 'batVolt', 'value': 399.1},
                    {'unit': 'A', 'name': 'BatCurrent', 'variable': 'batCurrent', 'value': 3.9},
                    {'unit': 'kW', 'name': 'MeterPower', 'variable': 'meterPower', 'value': 9.053},
                    {'unit': 'kW', 'name': 'Meter2Power', 'variable': 'meterPower2', 'value': 0.0},
                    {'unit': '%', 'name': 'SoC', 'variable': 'SoC', 'value': 26.0},
                    {'unit': 'kWh', 'name': 'Cumulative power generation', 'variable': 'generation', 'value': 6133.3},
                    {'unit': '0.01kWh', 'name': 'Battery Residual Energy', 'variable': 'ResidualEnergy', 'value': 10.34},
                    {'name': 'Running State', 'variable': 'runningState', 'value': '163'},
                    {'name': 'Battery Status', 'variable': 'batStatus', 'value': '1'},
                    {'name': 'Battery Status Name', 'variable': 'batStatusV2', 'value': 'Charge'},
                    {'name': 'The current error code is reported', 'variable': 'currentFault', 'value': ''},
                    {'name': 'The number of errors', 'variable': 'currentFaultCount', 'value': '0'},
                    {'unit': 'kWh', 'name': 'Battery throughput', 'variable': 'energyThroughput', 'value': 2255.872},
                    {'unit': '%', 'name': 'SOH', 'variable': 'SOH', 'value': 99.0},
                    {'unit': 'kWh', 'name': 'Total grid electricity consumption', 'variable': 'gridConsumption', 'value': 1712.7},
                    {'unit': 'kWh', 'name': 'Load power consumption', 'variable': 'loads', 'value': 4012.8},
                    {'unit': 'kWh', 'name': 'The total energy of the feeder', 'variable': 'feedin', 'value': 3730.6},
                    {'unit': 'kWh', 'name': 'Total charge energy', 'variable': 'chargeEnergyToTal', 'value': 1061.0},
                    {'unit': 'kWh', 'name': 'Total discharge energy', 'variable': 'dischargeEnergyToTal', 'value': 1532.6},
                    {'unit': "kWh", "name": "Photovoltaic power generation","variable": "PVEnergyTotal","value": 10291.7}
                ],
                'time': '2025-09-14 18:43:09 BST+0100', 'deviceSN': '60KE8020479C034'}
        ]
        """
        GET_REAL_TIME_DATA = "/op/v1/device/real/query"
        query = {"lang": FOX_LANG, "sns": [deviceSN]}
        result = await self.request_get(GET_REAL_TIME_DATA, post=True, datain=query)
        if result is not None and isinstance(result, list):
            for item in result:
                if "datas" in item:
                    timestamp = item.get("time", "")
                    datas = item["datas"]
                    for data_item in datas:
                        unit = data_item.get("unit", "")
                        name = data_item.get("name", "")
                        variable = data_item.get("variable", "")
                        value = data_item.get("value", None)
                        if unit == "℃":
                            unit = "°C"
                        if name and (value is not None):
                            if deviceSN not in self.device_values:
                                self.device_values[deviceSN] = {}
                            self.device_values[deviceSN][variable] = {"timestamp": timestamp, "value": value, "unit": unit, "name": name}

    async def get_device_history(self, deviceSN):
        """
        Get device history
        """
        GET_DEVICE_HISTORY = "/op/v0/device/history/query"
        timestamp = round(time.time() * 1000)
        query = {"sn": deviceSN, "begin": timestamp - 1000 * 60 * 60 * 1, "end": timestamp}
        result = await self.request_get(GET_DEVICE_HISTORY, post=True, datain=query)
        if result is not None and isinstance(result, list):
            for item in result:
                if "datas" in item:
                    datas = item["datas"]
                    for data_item in datas:
                        unit = data_item.get("unit", "")
                        name = data_item.get("name", "")
                        variable = data_item.get("variable", "")
                        history = data_item.get("data", [])
                        point = history[-1] if history else {}
                        timestamp = point.get("time", "")
                        value = point.get("value", None)
                        if unit == "℃":
                            unit = "°C"
                        if timestamp and variable and value is not None:
                            if deviceSN not in self.device_values:
                                self.device_values[deviceSN] = {}
                            self.device_values[deviceSN][variable] = {"timestamp": timestamp, "value": value, "unit": unit, "name": name}

    async def get_device_detail(self, deviceSN):
        """
        Get device information

        {
            'deviceType': 'KH8',
            'masterVersion': '1.34',
            'afciVersion': '',
            'hasPV': True,
            'deviceSN':
            '1234567890ABCDE',
            'slaveVersion': '1.01',
            'capacity': 8,
            'hasBattery': True,
            'function': {'scheduler': True},

            'hardwareVersion': '--',
            'managerVersion': '1.28',
            'stationName': 'My Home',
            'moduleSN': '12348020479C034',
            'batteryList':
                [{'batterySN': 'YYYYY', 'model': 'EP11', 'type': 'bcu', 'version': '1.005'},
                 {'batterySN': 'YYYYY', 'model': 'EP11', 'type': 'bmu', 'version': '1.05', 'capacity': 10360},
                 {'batterySN': 'YYYYY', 'model': 'EP11', 'type': 'ivu', 'version': '0.00'}],
            'productType': 'KH',
            'stationID': '23123-213123-231329',
            'status': 1
        }
        """
        GET_DEVICE_INFO = f"/op/v0/device/detail"
        query = {"sn": deviceSN}
        result = await self.request_get(GET_DEVICE_INFO, post=False, datain=query)
        if result is not None:
            self.device_detail[deviceSN] = result

    async def get_device_settings(self, deviceSN, checkBattery=True):
        """
        Get device settings
        """
        # Check if device has battery
        if checkBattery and not self.device_detail.get(deviceSN, {}).get("hasBattery", False):
            # These controls don't exist for non-battery devices
            return {}
        for key in FOX_SETTINGS:
            await self.get_device_setting(deviceSN, key)
        return self.device_settings.get(deviceSN, {})

    async def get_device_setting(self, deviceSN, key):
        """
        Get device setting
        {'enumList': ['PeakShaving', 'Feedin', 'SelfUse'], 'unit': '', 'precision': 1.0, 'value': 'SelfUse'}
        """
        GET_DEVICE_SETTING = "/op/v0/device/setting/get"
        result = await self.request_get(GET_DEVICE_SETTING, datain={"sn": deviceSN, "key": key}, post=True)
        if result is not None:
            if deviceSN not in self.device_settings:
                self.device_settings[deviceSN] = {}
            self.device_settings[deviceSN][key] = result
            return result
        else:
            self.log(f"Fox: Warn: Failed to get device setting for {deviceSN} key {key}")
        return None

    async def set_device_setting(self, deviceSN, key, value):
        """
        Set device setting
        """
        SET_DEVICE_SETTING = "/op/v0/device/setting/set"
        result = await self.request_get(SET_DEVICE_SETTING, datain={"sn": deviceSN, "key": key, "value": value, "lang": FOX_LANG}, post=True)
        if result is None:
            if self.device_settings.get(deviceSN, {}).get(key, None) is None:
                # Failed to write setting after failure to read, assume it doesn't exist
                self.log(f"Fox: Warn: Failed to set device setting for {deviceSN} key {key} value {value}, assuming not supported")
                return True
            return False
        return True

    async def set_battery_charging_time(self, deviceSN, setting):
        """
        Set battery charging time
        """
        SET_BATTERY_CHARGING_TIME = "/op/v0/device/battery/forceChargeTime/set"
        datain = {"sn": deviceSN}
        datain.update(setting)
        result = await self.request_get(SET_BATTERY_CHARGING_TIME, datain=datain, post=True)
        if result is None:
            return False
        return True

    async def get_battery_charging_time(self, deviceSN):
        """
        {
            'enable2': True,
            'endTime1': {'hour': 23, 'minute': 59},
            'enable1': True,
            'endTime2': {'hour': 5, 'minute': 30},
            'startTime2': {'hour': 0, 'minute': 0},
            'startTime1': {'hour': 23, 'minute': 30}
        }

        """
        GET_BATTERY_CHARGING_TIME = "/op/v0/device/battery/forceChargeTime/get"
        # Check if we have a battery
        if not self.device_detail.get(deviceSN, {}).get("hasBattery", False):
            # These controls don't exist for non-battery devices
            return {}
        result = await self.request_get(GET_BATTERY_CHARGING_TIME, datain={"sn": deviceSN}, post=False)
        if result is not None:
            self.device_battery_charging_time[deviceSN] = result
            return result
        return {}

    async def compute_schedule(self, deviceSN):
        """
        Work out the current schedule by looking at battery charging times or scheduler settings
        """
        battery_times = self.device_battery_charging_time.get(deviceSN, {})
        scheduler_times = self.device_scheduler.get(deviceSN, {}).get("groups", [])
        device_scheduler_enabled = self.device_scheduler.get(deviceSN, {}).get("enable", False)

        # First convert battery times into the same format as scheduler times
        # Create an array of 0 - 2 slots containing the battery charge times

        fdsoc_min = self.fdsoc_min.get(deviceSN, 10)
        reserve = self.local_schedule.get(deviceSN, {}).get("reserve", fdsoc_min)
        reserve = max(reserve, fdsoc_min)

        battery_slots = []
        for i in range(0, 8):
            if i < len(scheduler_times):
                battery_slots.append(scheduler_times[i].copy())
            else:
                battery_slots.append(
                    {
                        "startHour": 0,
                        "startMinute": 0,
                        "endHour": 0,
                        "endMinute": 0,
                        "enable": 0,
                        "fdPwr": self.fdpwr_max.get(deviceSN, 8000),
                        "workMode": "SelfUse",
                        "fdSoc": 100,
                        "minSocOnGrid": reserve,
                        "maxSoc": 100,
                    }
                )

        if not device_scheduler_enabled:
            for i in [1, 2]:
                start_time = battery_times.get(f"startTime{i}", {})
                end_time = battery_times.get(f"endTime{i}", {})
                enable = battery_times.get(f"enable{i}", False)
                if start_time and end_time and enable:
                    battery_slots[i - 1] = {
                        "startHour": start_time.get("hour", 0),
                        "startMinute": start_time.get("minute", 0),
                        "endHour": end_time.get("hour", 0),
                        "endMinute": end_time.get("minute", 0),
                        "enable": 1,
                        "fdPwr": 0,
                        "workMode": "ForceCharge",
                        "fdSoc": 100,
                        "minSocOnGrid": reserve,
                        "maxSoc": 100,
                    }
        self.device_current_schedule[deviceSN] = battery_slots

        # Sort the groups so that group 0 is the first charge slot and group 1 is the first discharge slot
        # For multiple slots pick the enabled one first
        charge_group = {}
        discharge_group = {}
        groups = battery_slots
        # Charge slot 0 if enabled
        for group in groups[:]:
            if group.get("enable", 0) and (group.get("workMode", "") in ["ForceCharge"]):
                charge_group = group
                break
        # For force discharge enabled
        for group in groups[:]:
            if group.get("enable", 0) and (group.get("workMode", "") in ["ForceDischarge"]):
                discharge_group = group
                break

        if deviceSN not in self.local_schedule:
            self.local_schedule[deviceSN] = {}

        if charge_group:
            end_hour = charge_group.get("endHour", 0)
            end_minute = charge_group.get("endMinute", 0)
            end_hour, end_minute = end_minute_inclusive_to_exclusive(end_hour, end_minute)

            self.local_schedule[deviceSN]["charge"] = {}
            self.local_schedule[deviceSN]["charge"]["start_time"] = "{:02d}:{:02d}:00".format(charge_group.get("startHour", 0), charge_group.get("startMinute", 0))
            self.local_schedule[deviceSN]["charge"]["end_time"] = "{:02d}:{:02d}:00".format(end_hour, end_minute)
            self.local_schedule[deviceSN]["charge"]["soc"] = charge_group.get("maxSoc", 100)
            self.local_schedule[deviceSN]["charge"]["power"] = self.fdpwr_max[deviceSN]
            self.local_schedule[deviceSN]["charge"]["enable"] = 1 if charge_group.get("enable", 0) else 0
        if discharge_group:
            end_hour = discharge_group.get("endHour", 0)
            end_minute = discharge_group.get("endMinute", 0)
            end_hour, end_minute = end_minute_inclusive_to_exclusive(end_hour, end_minute)
            self.local_schedule[deviceSN]["discharge"] = {}
            self.local_schedule[deviceSN]["discharge"]["start_time"] = "{:02d}:{:02d}:00".format(discharge_group.get("startHour", 0), discharge_group.get("startMinute", 0))
            self.local_schedule[deviceSN]["discharge"]["end_time"] = "{:02d}:{:02d}:00".format(end_hour, end_minute)
            self.local_schedule[deviceSN]["discharge"]["soc"] = discharge_group.get("fdSoc", 100)
            self.local_schedule[deviceSN]["discharge"]["power"] = int(discharge_group.get("fdPwr", 0))
            self.local_schedule[deviceSN]["discharge"]["enable"] = 1 if discharge_group.get("enable", 0) else 0
        return self.local_schedule

    async def get_device_production_year(self, deviceSN):
        """
        [
            {'unit': 'kWh', 'values': [0.0, 0.0, 0.0, 0.0, 151.5999999999999, 1079.1000000000004, 979.8999999999996, 871.3999999999987, 0.0, 0.0, 0.0, 0.0], 'variable': 'generation'},
            {'unit': 'kWh', 'values': [0.0, 0.0, 0.0, 0.0, 68.59999999999991, 685.2, 584.0, 534.3000000000002, 0.0, 0.0, 0.0, 0.0], 'variable': 'feedin'},
            {'unit': 'kWh', 'values': [0.0, 0.0, 0.0, 0.0, 52.700000000000045, 300.0999999999999, 295.2999999999997, 174.89999999999986, 0.0, 0.0, 0.0, 0.0], 'variable': 'gridConsumption'},
            {'unit': 'kWh', 'values': [0.0, 0.0, 0.0, 0.0, 36.30000000000007, 149.0, 170.0, 142.39999999999998, 0.0, 0.0, 0.0, 0.0], 'variable': 'chargeEnergyToTal'},
            {'unit': 'kWh', 'values': [0.0, 0.0, 0.0, 0.0, 52.600000000000136, 219.30000000000018, 253.1999999999997, 225.20000000000027, 0.0, 0.0, 0.0, 0.0], 'variable': 'dischargeEnergyToTal'}
        ]
        """
        GET_DEVICE_PRODUCTION = "/op/v0/device/report/query"
        year = datetime.now(self.local_tz).year
        variables = ["generation", "feedin", "gridConsumption", "chargeEnergyToTal", "dischargeEnergyToTal"]
        result = await self.request_get(GET_DEVICE_PRODUCTION, datain={"sn": deviceSN, "year": year, "dimension": "year", "variables": variables}, post=True)
        if result is not None:
            self.device_production_year[deviceSN] = result

    async def get_device_production_month(self, deviceSN):
        """
        [
            {"unit":"kWh","values":[0.0,0.0,0.0,0.0,0.0,0.1000000000003638,0.3999999999996362,0.5,0.3999999999996362,0.3999999999996362,0.3000000000010914,1.1000000000003638,1.5,1.2999999999992724,0.7000000000007276,0.5,0.4000000000014552,0.5,2.600000000000364,0.5,0.7999999999992724,0.7000000000007276,0.5,0.0],"variable":"generation"},
            {"unit":"kWh","values":[0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.8999999999996362,1.199999999999818,0.6999999999998181,0.3000000000001819,0.0,0.0,0.0,1.800000000000182,0.0,0.0,0.0,0.0,0.0],"variable":"feedin"},
            {"unit":"kWh","values":[11.399999999999636,1.3000000000010914,5.100000000000364,6.800000000001091,6.799999999999272,1.6000000000003638,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.09999999999854481,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.2000000000007276],"variable":"gridConsumption"},
            {"unit":"kWh","values":[4.800000000000182,0.8999999999996362,0.0,0.1000000000003638,0.0,0.0,0.0,0.0,0.0,0.1999999999998181,1.0,0.5,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0],"variable":"chargeEnergyToTal"},
            {"unit":"kWh","values":[0.0,0.0,0.0,0.0,0.0,0.1999999999998181,0.3999999999996362,0.6000000000003638,0.3999999999996362,0.2000000000007276,0.0,0.0,0.0,0.0999999999994543,0.0,0.3000000000001819,0.5,0.5,2.699999999999818,0.5,0.7999999999992724,0.8000000000001819,0.5,0.1000000000003638],"variable":"dischargeEnergyToTal"},
            {"unit":"kWh","values":[0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.3999999999996362,1.3999999999996362,1.6999999999989086,1.6000000000003638,1.2999999999992724,0.7999999999992724,0.2000000000007276,0.1000000000003638,0.0,0.0,0.0,0.0,0.0,0.0,0.0],"variable":"PVEnergyTotal"}
        ]
        """
        GET_DEVICE_PRODUCTION = "/op/v0/device/report/query"
        year = datetime.now(self.local_tz).year
        month = datetime.now(self.local_tz).month
        variables = ["generation", "feedin", "gridConsumption", "chargeEnergyToTal", "dischargeEnergyToTal", "PVEnergyTotal"]
        result = await self.request_get(GET_DEVICE_PRODUCTION, datain={"sn": deviceSN, "year": year, "month": month, "dimension": "month", "variables": variables}, post=True)
        if result is not None:
            self.device_production_month[deviceSN] = result

    async def get_device_power_generation(self, deviceSN):
        """
        {'month': 867.5999999999995, 'today': 17.699999999999818, 'cumulative': 5765.7}
        """
        GET_DEVICE_POWER = "/op/v0/device/generation"
        result = await self.request_get(GET_DEVICE_POWER, datain={"sn": deviceSN})
        if result is not None:
            self.device_power_generation[deviceSN] = result

    async def set_scheduler_enabled(self, deviceSN, enabled):
        """
        Set scheduler enabled/disabled
        """
        enabled_value = 1 if enabled else 0

        # Do change enable if not already modified
        if self.device_scheduler.get(deviceSN, {}).get("enable", None) == enabled_value:
            self.log("Fox: Debug: Scheduler for {} already set to enabled {}".format(deviceSN, enabled))
            return

        self.log("Fox: Debug: Setting scheduler enabled={} was {} for {}".format(enabled, self.device_scheduler.get(deviceSN, {}).get("enable", None), deviceSN))

        SET_SCHEDULER_ENABLED = "/op/v1/device/scheduler/set/flag"
        result = await self.request_get(SET_SCHEDULER_ENABLED, datain={"deviceSN": deviceSN, "enable": enabled_value}, post=True)
        if result is not None:
            if deviceSN not in self.device_scheduler:
                self.device_scheduler[deviceSN] = {}
            self.device_scheduler[deviceSN]["enable"] = enabled_value

    async def set_scheduler(self, deviceSN, groups):
        """
        Set scheduler groups, also disables scheduler if no groups provided
        """
        SET_SCHEDULER = "/op/v1/device/scheduler/enable"
        current_enable = self.device_scheduler.get(deviceSN, {}).get("enable", None)
        current_groups = self.device_scheduler.get(deviceSN, {}).get("groups", [])
        if not groups:
            if current_enable:
                # Disable scheduler if enabled and no groups
                await self.set_scheduler_enabled(deviceSN, False)
        else:
            # Compare old and new schedule to see if it needs setting
            same = schedules_are_equal(datetime.now(), current_groups, groups)
            self.log("Fox: Debug: Setting scheduler for {} same={} current_enable={} current_groups={} new_groups={}".format(deviceSN, same, current_enable, current_groups, groups))
            if not same:
                result = await self.request_get(SET_SCHEDULER, datain={"deviceSN": deviceSN, "groups": groups}, post=True)
                if result is not None:
                    if deviceSN not in self.device_scheduler:
                        self.device_scheduler[deviceSN] = {}
                    self.device_scheduler[deviceSN]["enable"] = True
                    self.device_scheduler[deviceSN]["groups"] = groups

    async def publish_schedule_settings_ha(self, deviceSN):
        """
        Publish the schedule settings to HA
        """
        # Device must have battery to publish settings
        if not self.device_detail.get(deviceSN, {}).get("hasBattery", False):
            return

        fdsoc_min = self.fdsoc_min.get(deviceSN, 10)
        local_schedule = self.local_schedule.get(deviceSN, {})

        # Global schedule control
        for attribute in ["reserve"]:
            entity_id_number = f"number.{self.prefix}_fox_{deviceSN.lower()}_battery_schedule_{attribute}"
            value = local_schedule.get(attribute, 0)
            if attribute == "reserve":
                self.dashboard_item(
                    entity_id_number,
                    state=value,
                    attributes={"min": fdsoc_min, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": "Fox {} Battery Schedule {}".format(deviceSN, attribute.replace("_", " ").capitalize()), "icon": "mdi:gauge"},
                    app="fox",
                )

        # Per direction schedule control
        for direction in ["charge", "discharge"]:
            for attribute in ["start_time", "end_time", "soc", "enable", "power", "write"]:
                entity_id_select = f"select.{self.prefix}_fox_{deviceSN.lower()}_battery_schedule_{direction}_{attribute}"
                entity_id_number = f"number.{self.prefix}_fox_{deviceSN.lower()}_battery_schedule_{direction}_{attribute}"
                entity_id_switch = f"switch.{self.prefix}_fox_{deviceSN.lower()}_battery_schedule_{direction}_{attribute}"

                if attribute in ["start_time", "end_time"]:
                    value = local_schedule.get(direction, {}).get(attribute, "00:00:00")
                    if value not in OPTIONS_TIME_FULL:
                        value = "00:00:00"
                    self.dashboard_item(
                        entity_id_select,
                        state=value,
                        attributes={"options": OPTIONS_TIME_FULL, "friendly_name": "Fox {} Battery Schedule {} {}".format(deviceSN, direction.capitalize(), attribute.replace("_", " ").capitalize()), "icon": "mdi:clock-outline"},
                        app="fox",
                    )
                elif attribute in ["soc", "power"]:
                    value = local_schedule.get(direction, {}).get(attribute, 0)
                    try:
                        value = int(float(value))
                    except ValueError:
                        value = 0
                    if attribute == "soc":
                        self.dashboard_item(
                            entity_id_number,
                            state=value,
                            attributes={
                                "min": fdsoc_min,
                                "max": 100,
                                "step": 1,
                                "unit_of_measurement": "%",
                                "friendly_name": "Fox {} Battery Schedule {} {}".format(deviceSN, direction.capitalize(), attribute.replace("_", " ").capitalize()),
                                "icon": "mdi:gauge",
                            },
                            app="fox",
                        )
                    elif attribute == "power":
                        max_power = self.fdpwr_max.get(deviceSN, 8000)
                        self.dashboard_item(
                            entity_id_number,
                            state=value,
                            attributes={
                                "min": 0,
                                "max": max_power,
                                "step": 100,
                                "unit_of_measurement": "W",
                                "friendly_name": "Fox {} Battery Schedule {} {}".format(deviceSN, direction.capitalize(), attribute.replace("_", " ").capitalize()),
                                "icon": "mdi:flash",
                            },
                            app="fox",
                        )
                elif attribute == "enable":
                    value = local_schedule.get(direction, {}).get(attribute, 0)
                    self.dashboard_item(
                        entity_id_switch,
                        state="on" if value else "off",
                        attributes={"friendly_name": "Fox {} Battery Schedule {} {}".format(deviceSN, direction.capitalize(), attribute.replace("_", " ").capitalize()), "icon": "mdi:check-circle-outline"},
                        app="fox",
                    )
                elif attribute == "write":
                    # Write button - always off
                    value = False
                    self.dashboard_item(
                        entity_id_switch,
                        state="on" if value else "off",
                        attributes={"friendly_name": "Fox {} Battery Schedule {} {}".format(deviceSN, direction.capitalize(), attribute.replace("_", " ").capitalize()), "icon": "mdi:check-circle-outline"},
                        app="fox",
                    )

    async def get_schedule_settings_ha(self, deviceSN):
        """
        Get the current schedule from HA database
        """
        fdsoc_min = self.fdsoc_min.get(deviceSN, 10)
        if deviceSN not in self.local_schedule:
            self.local_schedule[deviceSN] = {}
        for attribute in ["reserve"]:
            entity_id_number = f"number.{self.prefix}_fox_{deviceSN.lower()}_battery_schedule_{attribute}"
            value = self.get_state_wrapper(entity_id_number, default=0)
            try:
                value = int(float(value))
            except ValueError:
                value = fdsoc_min
            value = max(value, fdsoc_min)
            self.local_schedule[deviceSN][attribute] = value

        for direction in ["charge", "discharge"]:
            if direction not in self.local_schedule[deviceSN]:
                self.local_schedule[deviceSN][direction] = {}
            for attribute in ["start_time", "end_time", "soc", "enable", "power"]:
                entity_id_select = f"select.{self.prefix}_fox_{deviceSN.lower()}_battery_schedule_{direction}_{attribute}"
                entity_id_number = f"number.{self.prefix}_fox_{deviceSN.lower()}_battery_schedule_{direction}_{attribute}"
                entity_id_switch = f"switch.{self.prefix}_fox_{deviceSN.lower()}_battery_schedule_{direction}_{attribute}"

                if attribute in ["start_time", "end_time"]:
                    value = self.get_state_wrapper(entity_id_select, default="00:00:00")
                    self.local_schedule[deviceSN][direction][attribute] = value
                elif attribute in ["soc", "power"]:
                    default_value = 0
                    if attribute == "soc" and direction == "charge":
                        default_value = 100
                    elif attribute == "soc" and direction == "discharge":
                        default_value = self.fdsoc_min.get(deviceSN, 10)
                    elif attribute == "power":
                        default_value = self.fdpwr_max.get(deviceSN, 8000)
                    value = self.get_state_wrapper(entity_id_number, default=default_value)
                    try:
                        value = int(float(value))
                    except ValueError:
                        value = 0
                    self.local_schedule[deviceSN][direction][attribute] = value
                elif attribute == "enable":
                    value = self.get_state_wrapper(entity_id_switch, default="off")
                    self.local_schedule[deviceSN][direction][attribute] = 1 if value == "on" else 0

    async def get_scheduler(self, deviceSN, checkBattery=True):
        """
        Get device scheduler
        {
            'enable': 0,
            'groups':
                [
                    {'endHour': 0, 'fdPwr': 0, 'minSocOnGrid': 10, 'workMode': 'Invalid', 'fdSoc': 10, 'enable': 0, 'startHour': 0, 'maxSoc': 100, 'startMinute': 0, 'endMinute': 0},
                    {'endHour': 0, 'fdPwr': 0, 'minSocOnGrid': 10, 'workMode': 'Invalid', 'fdSoc': 10, 'enable': 0, 'startHour': 0, 'maxSoc': 100, 'startMinute': 0, 'endMinute': 0},
                    {'endHour': 0, 'fdPwr': 0, 'minSocOnGrid': 10, 'workMode': 'Invalid', 'fdSoc': 10, 'enable': 0, 'startHour': 0, 'maxSoc': 100, 'startMinute': 0, 'endMinute': 0},
                    {'endHour': 0, 'fdPwr': 0, 'minSocOnGrid': 10, 'workMode': 'Invalid', 'fdSoc': 10, 'enable': 0, 'startHour': 0, 'maxSoc': 100, 'startMinute': 0, 'endMinute': 0},
                    {'endHour': 0, 'fdPwr': 0, 'minSocOnGrid': 10, 'workMode': 'Invalid', 'fdSoc': 10, 'enable': 0, 'startHour': 0, 'maxSoc': 100, 'startMinute': 0, 'endMinute': 0},
                    {'endHour': 0, 'fdPwr': 0, 'minSocOnGrid': 10, 'workMode': 'Invalid', 'fdSoc': 10, 'enable': 0, 'startHour': 0, 'maxSoc': 100, 'startMinute': 0, 'endMinute': 0},
                    {'endHour': 0, 'fdPwr': 0, 'minSocOnGrid': 10, 'workMode': 'Invalid', 'fdSoc': 10, 'enable': 0, 'startHour': 0, 'maxSoc': 100, 'startMinute': 0, 'endMinute': 0},
                    {'endHour': 0, 'fdPwr': 0, 'minSocOnGrid': 10, 'workMode': 'Invalid', 'fdSoc': 10, 'enable': 0, 'startHour': 0, 'maxSoc': 100, 'startMinute': 0, 'endMinute': 0}
                ],
            'properties':
                {
                    'startminute': {'unit': '', 'precision': 1.0, 'range': {'min': 0.0, 'max': 59.0}},
                    'fdpwr': {'unit': 'W', 'precision': 1.0, 'range': {'min': 0.0, 'max': 10500.0}},
                    'endhour': {'unit': '', 'precision': 1.0, 'range': {'min': 0.0, 'max': 23.0}},
                    'endminute': {'unit': '', 'precision': 1.0, 'range': {'min': 0.0, 'max': 59.0}},
                    'fdsoc': {'unit': '%', 'precision': 1.0, 'range': {'min': 10.0, 'max': 100.0}},
                    'starthour': {'unit': '', 'precision': 1.0, 'range': {'min': 0.0, 'max': 23.0}},
                    'workmode': {'enumList': ['ForceDischarge', 'Feedin', 'SelfUse', 'ForceCharge'], 'unit': '', 'precision': 1.0},
                    'minsocongrid': {'unit': '%', 'precision': 1.0, 'range': {'min': 10.0, 'max': 100.0}},
                    'maxsoc': {'unit': '%', 'precision': 1.0, 'range': {'min': 10.0, 'max': 100.0}}
                }
        }
        """
        GET_SCHEDULER = "/op/v1/device/scheduler/get"

        # Only for battery devices
        if checkBattery and not self.device_detail.get(deviceSN, {}).get("hasBattery", False):
            return {}

        detail = self.device_detail.get(deviceSN, {})
        inverter_capacity = detail.get("capacity", 0) * 1000.0

        result = await self.request_get(GET_SCHEDULER, datain={"deviceSN": deviceSN}, post=True)
        if result is not None:
            self.fdpwr_max[deviceSN] = result.get("properties", {}).get("fdpwr", {}).get("range", {}).get("max", 8000)
            # XXX: Fox seems to be have an issue with FD Power max value being too high, cap it at the inverter capacity
            if inverter_capacity:
                self.fdpwr_max[deviceSN] = min(inverter_capacity, self.fdpwr_max[deviceSN])

            # Min SOC On grid can change as Predbat writes reserve so this must be the real min
            self.fdsoc_min[deviceSN] = result.get("properties", {}).get("fdsoc", {}).get("range", {}).get("min", 10)
            self.log("Fox: Fetched schedule got {} fdPwr max {} fdSoc min {}".format(result, self.fdpwr_max[deviceSN], self.fdsoc_min[deviceSN]))
            self.device_scheduler[deviceSN] = result
            return result
        return {}

    async def get_device_list(self):
        """
        [
            {
            'deviceType': 'KH8',
            'hasBattery': True,
            'hasPV': True,
            'stationName':
            '2 Dona Fold',
            'moduleSN': '609W6EUF46MB519',
            'deviceSN': '60KE8020479C034',
            'productType': 'KH',
            'stationID': '2958ff16-13a5-4ab9-957a-79e938f86a19',
            'status': 1
            }
        ]
        """
        GET_DEVICE_LIST = "/op/v0/device/list"
        query = {"pageSize": 100, "currentPage": 1}
        result = await self.request_get(GET_DEVICE_LIST, post=True, datain=query)
        devices = []
        if result is not None:
            devices = result.get("data", [])
            # If self.inverter_sn_filter is set, keep only devices whose deviceSN is in that filter
            if self.inverter_sn_filter:
                devices = [device for device in devices if device.get("deviceSN", "") in self.inverter_sn_filter]
            self.device_list = devices
        return devices

    def get_headers(self, path):
        headers = {}
        token = self.key
        lang = FOX_LANG
        timestamp = str(round(time.time() * 1000))
        headers["token"] = token
        headers["lang"] = lang
        headers["timestamp"] = timestamp
        signature = rf"{path}\r\n{token}\r\n{timestamp}"
        headers["signature"] = hashlib.md5(signature.encode("UTF-8")).hexdigest()
        return headers

    async def request_get(self, path, post=False, datain=None):
        """
        Retry wrapper
        """
        retries = 0
        self.log("Fox: API Requesting {} {} - data {}".format("POST" if post else "GET", path, datain))

        while retries < FOX_RETRIES:
            result, allow_retry = await self.request_get_func(path, post=post, datain=datain)
            if result is not None:
                return result
            if not allow_retry:
                break

            # Check if rate limiting prevents retry
            if not self.should_allow_retry():
                self.log("Fox: Retries disabled due to rate limiting (>60/hour average)")
                break

            retries += 1
            await asyncio.sleep(retries * random.random())
        self.log("Fox: API Response failed after {} retries for {}".format(FOX_RETRIES, path))
        return result

    async def request_get_func(self, path, post=False, datain=None):
        # Track API request
        self.requests_today += 1

        headers = self.get_headers(path)
        url = FOX_DOMAIN + path
        self.log("Fox: API Request: path {} post {} datain {}".format(path, post, datain))

        timeout = aiohttp.ClientTimeout(total=TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if post:
                    if datain:
                        async with session.post(url, headers=headers, json=datain) as response:
                            status_code = response.status
                            try:
                                data = await response.json()
                            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                                self.log("Warn: Fox: Failed to decode response from {} code {}".format(url, status_code))
                                data = None
                    else:
                        async with session.post(url, headers=headers) as response:
                            status_code = response.status
                            try:
                                data = await response.json()
                            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                                self.log("Warn: Fox: Failed to decode response from {} code {}".format(url, status_code))
                                data = None
                else:
                    async with session.get(url, headers=headers, params=datain) as response:
                        status_code = response.status
                        try:
                            data = await response.json()
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            self.log("Warn: Fox: Failed to decode response from {} code {}".format(url, status_code))
                            data = None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: Fox: Exception during request to {url}: {e}")
            self.failures_total += 1
            return None, False

        if status_code in [400, 401, 402, 403]:
            self.log("Warn: Fox: Authentication error with status code {} from {}".format(status_code, url))
            self.failures_total += 1
            return None, False

        if status_code in [200, 201]:
            if data is None:
                data = {}
            errno = data.get("errno", 0)
            msg = data.get("msg", "")
            if errno != 0:
                self.failures_total += 1
                if errno in [40400, 41200, 41201, 41202, 41203, 41935, 44098]:
                    # Rate limiting detected
                    self.rate_limit_errors_today += 1
                    self.log(f"Info: Fox: Rate limiting or comms issue detected {msg}:{errno}, waiting...")
                    await asyncio.sleep(random.random() * 30 + 1)
                    return None, True
                elif errno in [40402]:
                    # Out of API calls for today
                    self.log(f"Warn: Fox: Has run out of API calls for today {msg}:{errno}, sleeping...")
                    await asyncio.sleep(5 * 60)
                    return None, False
                elif errno in [44096, 42015]:
                    # Unsupported function code
                    self.log(f"Warn: Fox: Unsupported function code {msg}:{errno} from {url}")
                    return None, False
                elif errno in [40257]:
                    # Invalid parameter
                    self.log(f"Warn: Fox: Invalid parameter {msg}:{errno} from {url}")
                    return None, False
                else:
                    self.log("Warn: Fox: Error {} from {} message {}".format(errno, url, msg))
                return None, False

            if "result" in data:
                data = data["result"]
                if data is None:
                    data = {}

            self.update_success_timestamp()
            return data, False
        else:
            self.failures_total += 1
            if status_code == 429:
                # Rate limiting so wait up to 30 seconds
                self.log("Info: Fox: Rate limiting detected, waiting...")
                await asyncio.sleep(random.random() * 30 + 1)
                return None, True
        return None, False

    async def publish_data(self):
        """
        Publish data to HA using dashboard_item
        """

        # Create entity name prefix
        entity_name_sensor = f"sensor.{self.prefix}_fox"
        entity_name_number = f"number.{self.prefix}_fox"
        entity_name_select = f"select.{self.prefix}_fox"
        entity_name_switch = f"switch.{self.prefix}_fox"
        entity_name_binary_sensor = f"binary_sensor.{self.prefix}_fox"
        for device in self.device_list:
            sn = device.get("deviceSN", None)
            detail = self.device_detail.get(sn, {})
            hasPV = detail.get("hasPV", False)
            hasBattery = detail.get("hasBattery", False)
            capacity = detail.get("capacity", 0) * 1000.0
            hasScheduler = detail.get("function", {}).get("scheduler", False)
            deviceType = detail.get("deviceType", "Unknown")
            stationName = detail.get("stationName", "Unknown")
            batteryList = detail.get("batteryList", [])
            battery_capacity = 0
            for battery in batteryList:
                battery_capacity += battery.get("capacity", 0)

            self.dashboard_item(
                entity_name_sensor + "_" + sn.lower() + "_info",
                state=stationName,
                attributes={"friendly_name": f"Fox {sn} Info", "hasPV": hasPV, "hasBattery": hasBattery, "inverterCapacity": capacity, "batteryCapacity": battery_capacity, "hasScheduler": hasScheduler, "deviceType": deviceType, "stationName": stationName},
                app="fox",
            )
            if not hasBattery:
                capacity = 0
            self.dashboard_item(entity_name_sensor + "_" + sn.lower() + "_inverter_capacity", state=capacity, attributes={"friendly_name": f"Fox {sn} Inverter Capacity", "unit_of_measurement": "W"}, app="fox")
            self.dashboard_item(entity_name_sensor + "_" + sn.lower() + "_battery_capacity", state=battery_capacity / 1000.0, attributes={"friendly_name": f"Fox {sn} Battery Capacity", "unit_of_measurement": "kWh"}, app="fox")

            battery_rate_max = int(self.fdpwr_max.get(sn, 8000))
            self.dashboard_item(entity_name_sensor + "_" + sn.lower() + "_battery_rate_max", state=battery_rate_max, attributes={"friendly_name": f"Fox {sn} Battery Max Rate", "unit_of_measurement": "W"}, app="fox")

            reserve_min = int(self.fdsoc_min.get(sn, 10))
            self.dashboard_item(entity_name_sensor + "_" + sn.lower() + "_battery_reserve_min", state=reserve_min, attributes={"friendly_name": f"Fox {sn} Battery Reserve Min", "unit_of_measurement": "%"}, app="fox")

        # If we have soc_x sensors then sum them for total soc and store as _soc so that Predbat gets a single SOC value
        for sn in self.device_values:
            soc_total = 0
            soc_total_count = 0
            for item_name in self.device_values[sn]:
                if item_name.lower().startswith("soc_"):
                    item = self.device_values[sn][item_name]
                    soc = item.get("value", None)
                    try:
                        soc = float(soc)
                    except ValueError:
                        soc = None
                    if soc is not None:
                        soc_total += soc
                        soc_total_count += 1
            if soc_total_count > 0:
                # Remove the SOC dictionary key (any case) otherwise we might create a duplicate (different case)
                for key in list(self.device_values[sn].keys()):
                    if key.lower() == "soc":
                        del self.device_values[sn][key]
                # Add total SOC
                self.device_values[sn]["SoC"] = {"name": "State of Charge Total", "unit": "%", "value": round(soc_total / soc_total_count, 0)}

        # Publish device values
        for sn in self.device_values:
            for item_name in self.device_values[sn]:
                item = self.device_values[sn][item_name]
                state = item.get("value", None)
                name = item.get("name", item_name)
                units = item.get("unit", "")
                entity_id = entity_name_sensor + "_" + sn.lower() + "_" + item_name.lower()
                attributes = {
                    "unit_of_measurement": units,
                    "friendly_name": f"Fox {sn} {name}",
                }
                # Set device and state class
                if units in ["kWh", "Wh"]:
                    attributes["device_class"] = "energy"
                if units in ["kW"]:
                    attributes["device_class"] = "power"
                if units in ["V"]:
                    attributes["device_class"] = "voltage"
                if units in ["A"]:
                    attributes["device_class"] = "current"
                if units in ["kW", "W", "V", "A"]:
                    attributes["state_class"] = "measurement"
                if item_name.lower() in ["generation", "energythroughput", "gridconsumption", "loads", "feedin", "chargeenergytotal", "dischargeenergytotal", "pvenergytotal"]:
                    attributes["state_class"] = "total"
                self.dashboard_item(entity_id, state=state, attributes=attributes, app="fox")

            # Publish battery flow sensor
            charge_power = self.device_values.get(sn, {}).get("batChargePower", {}).get("value", 0)
            discharge_power = self.device_values.get(sn, {}).get("batDischargePower", {}).get("value", 0)

            try:
                charge_power = float(charge_power) if charge_power is not None else 0.0
                discharge_power = float(discharge_power) if discharge_power is not None else 0.0
            except (ValueError, TypeError):
                charge_power = 0.0
                discharge_power = 0.0

            # Calculate battery flow: positive = discharge, negative = charge
            battery_flow = discharge_power - charge_power

            self.dashboard_item(
                entity_name_sensor + "_" + sn.lower() + "_battery_flow",
                state=battery_flow,
                attributes={"friendly_name": f"Fox {sn} Battery Flow", "unit_of_measurement": "kW", "device_class": "power", "state_class": "measurement", "icon": "mdi:battery-arrow-up-down"},
                app="fox",
            )

            # Publish schedule settings
            await self.publish_schedule_settings_ha(sn)

        for sn in self.device_settings:
            # Device must have battery to publish settings
            if not self.device_detail.get(sn, {}).get("hasBattery", False):
                continue

            for setting in self.device_settings[sn]:
                item = self.device_settings[sn][setting]
                state = item.get("value", None)
                unit = item.get("unit", "")
                value_range = item.get("range", {})
                precision = item.get("precision", 1)
                enumList = item.get("enumList", [])

                name = setting
                attributes = {
                    "unit_of_measurement": unit,
                    "friendly_name": f"Fox {sn} {name}",
                }
                if enumList:
                    # Selector
                    attributes["options"] = enumList
                    entity_id = entity_name_select + "_" + sn.lower() + "_" + "setting_" + setting.lower()
                elif value_range:
                    # Number
                    attributes["min"] = value_range.get("min", 0)
                    attributes["max"] = value_range.get("max", 100)
                    attributes["step"] = precision
                    entity_id = entity_name_number + "_" + sn.lower() + "_" + "setting_" + setting.lower()
                else:
                    # Sensor
                    entity_id = entity_name_sensor + "_" + sn.lower() + "_" + "setting_" + setting.lower()
                self.dashboard_item(entity_id, state=state, attributes=attributes, app="fox")

        # Publish month and today totals
        for sn in self.device_production_month:
            today = datetime.now(self.local_tz).day

            for item in self.device_production_month[sn]:
                units = item.get("unit", "")
                variable = item.get("variable", "")
                values = item.get("values", [])

                # Month Total Sensor
                item_name = variable + " (Month)"
                entity_id = entity_name_sensor + "_" + sn.lower() + "_" + variable.lower() + "_month"
                state = sum(values)
                attributes = {"unit_of_measurement": units, "friendly_name": f"Fox {sn} {item_name}", "values": values}
                if units in ["kWh", "Wh"]:
                    attributes["device_class"] = "energy"
                if variable.lower() in ["generation", "feedin", "gridconsumption", "chargeenergytotal", "dischargeenergytotal", "pvenergytotal"]:
                    attributes["state_class"] = "total"

                self.dashboard_item(entity_id, state=state, attributes=attributes, app="fox")

                # Today Total Sensor
                item_name = variable + " (Today)"
                entity_id = entity_name_sensor + "_" + sn.lower() + "_" + variable.lower() + "_today"
                state = values[today - 1] if len(values) >= today else 0

                attributes = {
                    "unit_of_measurement": units,
                    "friendly_name": f"Fox {sn} {item_name}",
                }
                if units in ["kWh", "Wh"]:
                    attributes["device_class"] = "energy"
                if variable.lower() in ["generation", "feedin", "gridconsumption", "chargeenergytotal", "dischargeenergytotal", "pvenergytotal"]:
                    attributes["state_class"] = "total"

                self.dashboard_item(entity_id, state=state, attributes=attributes, app="fox")

    async def write_setting_from_event(self, entity_id, value, is_number=False):
        """
        Handle write events
        """
        entity_id = entity_id.replace(f"number.{self.prefix}_fox_", "")
        entity_id = entity_id.replace(f"select.{self.prefix}_fox_", "")
        entity_id = entity_id.replace(f"switch.{self.prefix}_fox_", "")
        entity_id = entity_id.replace("setting_", "")
        sn = entity_id.split("_")[0]
        register_lower = entity_id.split("_")[1]
        fox_settings_lower = [s.lower() for s in FOX_SETTINGS]
        serial = None
        for s in self.device_settings:
            if s.lower() == sn.lower():
                serial = s
                break
        if not serial:
            self.log("Warn: Fox: Event, unknown serial number for {}: {}".format(entity_id, sn))
            return
        if register_lower in fox_settings_lower:
            register = FOX_SETTINGS[fox_settings_lower.index(register_lower)]
            if is_number:
                step = self.device_settings[serial][register].get("precision", None)
                if step and step == 1:
                    try:
                        value = int(value)
                    except ValueError:
                        self.log("Warn: Fox: Invalid integer value for {}: {}".format(entity_id, value))
                        return
                else:
                    try:
                        value = float(value)
                    except ValueError:
                        self.log("Warn: Fox: Invalid number value for {}: {}".format(entity_id, value))
                        return
            if self.device_settings[serial].get(register, {}).get("value", None) != value:
                # Only write if value has changed
                if await self.set_device_setting(sn, register, value):
                    self.device_settings[serial][register]["value"] = value
        else:
            self.log("Warn: Fox: Unknown write event event for {} value {}".format(entity_id, value))
        await self.publish_data()

    async def select_event(self, entity_id, value):
        """
        Handle select events
        """
        if "_setting_" in entity_id:
            await self.write_setting_from_event(entity_id, value)
        elif "_battery_schedule_" in entity_id:
            await self.write_battery_schedule_event(entity_id, value)

    async def number_event(self, entity_id, value):
        if "_setting_" in entity_id:
            await self.write_setting_from_event(entity_id, value, is_number=True)
        elif "_battery_schedule_" in entity_id:
            await self.write_battery_schedule_event(entity_id, value)

    async def switch_event(self, entity_id, service):
        if "_battery_schedule_" in entity_id:
            await self.write_battery_schedule_event(entity_id, service)

    def apply_service_to_toggle(self, current_value, service):
        """
        Apply a toggle service to the current value.
        """
        if service == "turn_on":
            current_value = True
        elif service == "turn_off":
            current_value = False
        elif service == "toggle":
            current_value = not current_value
        return current_value

    def time_string_to_hour_minute(self, value, orig_hour, orig_minute):
        """
        Convert a time string in the format HH:MM to hour and minute integers.
        """
        split_up = value.split(":")
        if len(split_up) < 2:
            return orig_hour, orig_minute

        hour = split_up[0]
        minute = split_up[1]
        try:
            hour = int(hour)
            minute = int(minute)
        except ValueError:
            hour = orig_hour
            minute = orig_minute
        if hour < 0 or hour > 23:
            hour = orig_hour
        if minute < 0 or minute > 59:
            minute = orig_minute
        return hour, minute

    async def write_battery_schedule_event(self, entity_id, value):
        """
        Handle battery schedule events
        """

        entity_id = entity_id.replace(f"switch.{self.prefix}_fox_", "")
        entity_id = entity_id.replace(f"select.{self.prefix}_fox_", "")
        entity_id = entity_id.replace(f"number.{self.prefix}_fox_", "")
        sn = entity_id.split("_")[0]
        serial = None
        for s in self.device_current_schedule:
            if s.lower() == sn.lower():
                serial = s
                break

        if not serial:
            self.log("Warn: Fox: Event, unknown serial number for {}: {}".format(entity_id, sn))
            return

        if serial not in self.local_schedule:
            self.local_schedule[serial] = {}

        direction = ""
        direction = "charge" if "_charge_" in entity_id else direction
        direction = "discharge" if "_discharge_" in entity_id else direction

        # non-directional settings
        if "_reserve" in entity_id:
            try:
                value = int(value)
            except ValueError:
                value = self.fdsoc_min.get(serial, 10)
            value = max(value, self.fdsoc_min.get(serial, 10))
            self.local_schedule[serial]["reserve"] = value
            await self.publish_schedule_settings_ha(serial)
            # Changing reserve impacts idle slots so re-apply full schedule
            await self.apply_battery_schedule(serial)
            return

        if not direction:
            self.log("Warn: Fox: Event, unknown direction for {}: {}".format(entity_id, sn))
            return

        if direction not in self.local_schedule[serial]:
            self.local_schedule[serial][direction] = {}

        if "_soc" in entity_id:
            try:
                value = int(value)
            except ValueError:
                value = 100 if direction == "charge" else self.fdsoc_min.get(serial, 10)
            self.local_schedule[serial][direction]["soc"] = value
        elif "_power" in entity_id:
            try:
                value = int(value)
            except ValueError:
                value = self.fdpwr_max.get(serial, 8000)
            self.local_schedule[serial][direction]["power"] = value
        elif "_start_time" in entity_id:
            if value not in OPTIONS_TIME_FULL:
                value = "00:00:00"
            self.local_schedule[serial][direction]["start_time"] = value
        elif "_end_time" in entity_id:
            if value not in OPTIONS_TIME_FULL:
                value = "00:00:00"
            self.local_schedule[serial][direction]["end_time"] = value
        elif "_enable" in entity_id:
            enable = True if self.local_schedule[serial][direction].get("enable", 0) else False
            new_enable = 1 if self.apply_service_to_toggle(enable, value) else 0
            self.local_schedule[serial][direction]["enable"] = new_enable
        elif "_write" in entity_id:
            await self.apply_battery_schedule(serial)
        else:
            self.log("Warn: Fox: Event, unknown attribute for {}: {}".format(entity_id, serial))
            return

        await self.publish_schedule_settings_ha(serial)

    async def apply_battery_schedule(self, serial):
        new_schedule = []
        fdPwr_max = self.fdpwr_max.get(serial, 8000)
        fdsoc_min = self.fdsoc_min.get(serial, 10)
        reserve = self.local_schedule.get(serial, {}).get("reserve", fdsoc_min)
        reserve = max(reserve, fdsoc_min)

        for direction in ["charge", "discharge"]:
            enable = self.local_schedule[serial].get(direction, {}).get("enable", 0)
            if enable:
                start_time = self.local_schedule[serial].get(direction, {}).get("start_time", "00:00:00")
                end_time = self.local_schedule[serial].get(direction, {}).get("end_time", "00:00:00")
                soc = self.local_schedule[serial].get(direction, {}).get("soc", 100 if direction == "charge" else self.fdsoc_min.get(serial, 10))
                power = self.local_schedule[serial].get(direction, {}).get("power", fdPwr_max)

                start_hour, start_minute = self.time_string_to_hour_minute(start_time, 0, 0)
                end_hour, end_minute = self.time_string_to_hour_minute(end_time, 0, 0)

                if direction == "charge":
                    new_schedule.append(
                        {
                            "enable": 1,
                            "startHour": start_hour,
                            "startMinute": start_minute,
                            "endHour": end_hour,
                            "endMinute": end_minute,
                            "workMode": "ForceCharge",
                            "fdSoc": 100,
                            "maxSoc": soc,
                            "fdPwr": fdPwr_max,
                            "minSocOnGrid": soc,
                        }
                    )
                elif direction == "discharge":
                    new_schedule.append(
                        {"enable": 1, "startHour": start_hour, "startMinute": start_minute, "endHour": end_hour, "endMinute": end_minute, "workMode": "ForceDischarge", "fdSoc": max(soc, reserve), "maxSoc": reserve, "fdPwr": power, "minSocOnGrid": reserve}
                    )
        new_schedule = validate_schedule(new_schedule, reserve, fdPwr_max)
        self.log("Fox: New schedule for {}: {}".format(serial, new_schedule))
        result = await self.set_scheduler(serial, new_schedule)
        if result is not None:
            self.device_current_schedule[serial] = new_schedule
            await self.publish_data()

    async def automatic_config(self):
        """
        Automatically configure the base args based on the devices found
        """

        batteries = []
        pvs = []
        hasExportLimit = {}
        for device in self.device_list:
            sn = device.get("deviceSN", None)
            detail = self.device_detail.get(sn, {})
            hasPV = detail.get("hasPV", False)
            hasBattery = detail.get("hasBattery", False)
            capacity = detail.get("capacity", 0) * 1000.0
            hasScheduler = detail.get("function", {}).get("scheduler", False)

            if hasBattery and hasScheduler and capacity > 0:
                batteries.append(sn.lower())
                # Check if this battery inverter also has PV
                if hasPV:
                    pvs.append(sn.lower())

        for sn in self.device_settings:
            for setting in self.device_settings[sn]:
                if setting.lower() == "exportlimit":
                    hasExportLimit[sn.lower()] = True

        # Find any PV inverters without batteries when the battery doesn't see the PV
        if len(pvs) < len(batteries):
            for device in self.device_list:
                sn = device.get("deviceSN", None)
                detail = self.device_detail.get(sn, {})
                hasPV = detail.get("hasPV", False)
                hasBattery = detail.get("hasBattery", False)
                if hasPV and not hasBattery:
                    pvs.append(sn.lower())

        num_inverters = len(batteries)
        self.log("Fox API: Found {} batteries and {} PVs".format(num_inverters, len(pvs)))
        if not num_inverters:
            raise ValueError("Fox API: No batteries with scheduler found, cannot configure")

        self.set_arg("inverter_type", ["FoxCloud" for _ in range(num_inverters)])
        self.set_arg("num_inverters", num_inverters)
        self.set_arg("inverter_mode", [f"select.{self.prefix}_fox_{device}_setting_workmode" for device in batteries])
        self.set_arg("load_today", [f"sensor.{self.prefix}_fox_{device}_loads" for device in batteries])
        self.set_arg("import_today", [f"sensor.{self.prefix}_fox_{device}_gridconsumption" for device in batteries])
        self.set_arg("export_today", [f"sensor.{self.prefix}_fox_{device}_feedin" for device in batteries])
        self.set_arg("pv_today", [f"sensor.{self.prefix}_fox_{device}_pvenergytotal_today" for device in pvs])
        self.set_arg("battery_rate_max", [f"sensor.{self.prefix}_fox_{device}_battery_rate_max" for device in batteries])
        self.set_arg("battery_power", [f"sensor.{self.prefix}_fox_{device}_invbatpower" for device in batteries])
        self.set_arg("grid_power", [f"sensor.{self.prefix}_fox_{device}_meterpower" for device in batteries])
        self.set_arg("grid_power_invert", [True for device in batteries])
        self.set_arg("pv_power", [f"sensor.{self.prefix}_fox_{device}_pvpower" for device in pvs])
        self.set_arg("load_power", [f"sensor.{self.prefix}_fox_{device}_loadspower" for device in batteries])
        self.set_arg("soc_percent", [f"sensor.{self.prefix}_fox_{device}_soc" for device in batteries])
        self.set_arg("soc_max", [f"sensor.{self.prefix}_fox_{device}_battery_capacity" for device in batteries])
        self.set_arg("reserve", [f"number.{self.prefix}_fox_{device}_battery_schedule_reserve" for device in batteries])
        self.set_arg("battery_min_soc", [f"sensor.{self.prefix}_fox_{device}_battery_reserve_min" for device in batteries])
        self.set_arg("charge_start_time", [f"select.{self.prefix}_fox_{device}_battery_schedule_charge_start_time" for device in batteries])
        self.set_arg("charge_end_time", [f"select.{self.prefix}_fox_{device}_battery_schedule_charge_end_time" for device in batteries])
        self.set_arg("charge_limit", [f"number.{self.prefix}_fox_{device}_battery_schedule_charge_soc" for device in batteries])
        self.set_arg("scheduled_charge_enable", [f"switch.{self.prefix}_fox_{device}_battery_schedule_charge_enable" for device in batteries])
        self.set_arg("charge_rate", [f"number.{self.prefix}_fox_{device}_battery_schedule_charge_power" for device in batteries])
        self.set_arg("scheduled_discharge_enable", [f"switch.{self.prefix}_fox_{device}_battery_schedule_discharge_enable" for device in batteries])
        self.set_arg("discharge_target_soc", [f"number.{self.prefix}_fox_{device}_battery_schedule_discharge_soc" for device in batteries])
        self.set_arg("discharge_start_time", [f"select.{self.prefix}_fox_{device}_battery_schedule_discharge_start_time" for device in batteries])
        self.set_arg("discharge_end_time", [f"select.{self.prefix}_fox_{device}_battery_schedule_discharge_end_time" for device in batteries])
        self.set_arg("discharge_rate", [f"number.{self.prefix}_fox_{device}_battery_schedule_discharge_power" for device in batteries])
        self.set_arg("battery_temperature", [f"sensor.{self.prefix}_fox_{device}_battemperature" for device in batteries])
        self.set_arg("inverter_limit", [f"sensor.{self.prefix}_fox_{device}_inverter_capacity" for device in batteries])
        self.set_arg("schedule_write_button", [f"switch.{self.prefix}_fox_{device}_battery_schedule_charge_write" for device in batteries])
        self.set_arg("export_limit", [f"number.{self.prefix}_fox_{device}_setting_exportlimit" if hasExportLimit.get(device, False) else 99999 for device in batteries])

        if len(batteries):
            self.set_arg("battery_temperature_history", f"sensor.{self.prefix}_fox_{batteries[0]}_battemperature")


class MockBase:  # pragma: no cover
    """Mock base class for testing"""

    def __init__(self):
        self.local_tz = datetime.now().astimezone().tzinfo
        self.now_utc = datetime.now(self.local_tz)
        self.prefix = "predbat"
        self.args = {}
        self.midnight_utc = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = self.now_utc.hour * 60 + self.now_utc.minute
        self.entities = {}

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=None):
        if raw:
            return self.entities.get(entity_id, {})
        else:
            return self.entities.get(entity_id, {}).get("state", default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None):
        self.entities[entity_id] = {"state": state, "attributes": attributes or {}}

    def log(self, message):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        print(f"ENTITY: {entity_id} = {state}")
        if attributes:
            if "options" in attributes:
                attributes["options"] = "..."
            print(f"  Attributes: {json.dumps(attributes, indent=2)}")
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, key, default=None):
        return default

    def set_arg(self, key, value):
        state = None
        if isinstance(value, str) and "." in value:
            state = self.get_state_wrapper(value, default=None)
        elif isinstance(value, list):
            state = "n/a []"
            for v in value:
                if isinstance(v, str) and "." in v:
                    state = self.get_state_wrapper(v, default=None)
                    break
        else:
            state = "n/a"
        print(f"Set arg {key} = {value} (state={state})")


async def test_fox_api(sn, api_key):  # pragma: no cover
    """
    Run a test
    """
    print(f"Testing Fox API with key: {api_key[:10]}...")

    # Create a mock base object
    mock_base = MockBase()

    # Create FoxAPI instance with a lambda that returns the API key
    arg_dict = {}
    arg_dict = {"key": api_key, "automatic": True}
    fox_api = FoxAPI(mock_base, **arg_dict)

    # Call run() once
    print("Calling run() once...")
    await fox_api.run(seconds=0, first=True)
    print("Run completed successfully")


def main():  # pragma: no cover
    """
    Main function for command line execution
    """
    parser = argparse.ArgumentParser(description="Test Fox API")
    parser.add_argument("--serial", action="store_true", default=None, help="Fox API serial number")
    parser.add_argument("--api-key", required=True, help="Fox API key")

    args = parser.parse_args()
    key = args.api_key
    serial = args.serial

    # Run the test
    asyncio.run(test_fox_api(serial, key))


if __name__ == "__main__":
    main()
