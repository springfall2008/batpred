# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Fox API Library
# -----------------------------------------------------------------------------

import asyncio
from datetime import datetime, timedelta, timezone
import traceback
import time
import hashlib
import requests
import argparse
import random

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


class FoxAPI:
    """Fox API client."""

    def __init__(self, key, automatic, base):
        self.base = base
        self.log = base.log
        self.key = key
        self.api_started = False
        self.automatic = automatic
        self.stop_api = False
        self.failures_total = 0
        self.device_list = []
        self.device_detail = {}
        self.device_power_generation = {}
        self.available_variables = {}
        self.device_values = {}
        self.device_settings = {}
        self.device_production = {}
        self.device_battery_charging_time = {}
        self.device_scheduler = {}
        self.device_current_schedule = {}
        self.local_schedule = {}
        self.fdpwr_max = {}
        self.fdsoc_min = {}
        self.last_success_timestamp = None

    def wait_api_started(self):
        """
        Wait for the API to start
        """
        self.log("Fox API: Waiting for API to start")
        count = 0
        while not self.api_started and count < 240:
            time.sleep(1)
            count += 1
        if not self.api_started:
            self.log("Warn: Fox API: Failed to start")
            return False
        return True

    def is_alive(self):
        """
        Check if the API is alive
        """
        return self.api_started and self.device_list

    def last_updated_time(self):
        """
        Get the last successful update time
        """
        return self.last_success_timestamp

    async def start(self):
        """
        Main run loop
        """

        first = True
        count_seconds = 0
        while not self.stop_api:
            try:
                if first or (count_seconds % (60 * 60) == 0):
                    if first:
                        # Only do these once as battery charging times are ignored with the scheduler
                        # and we get the realtime data every 5 minutes
                        await self.get_device_list()
                        self.log("Fox API: Found {} devices".format(len(self.device_list)))

                        # Get per device data
                        for device in self.device_list:
                            sn = device.get("deviceSN", None)
                            if sn:
                                await self.get_device_detail(sn)
                                await self.get_device_history(sn)
                                await self.get_battery_charging_time(sn)

                    # Regular updates for registers and scheduler data
                    for device in self.device_list:
                        sn = device.get("deviceSN", None)
                        if sn:
                            await self.get_device_settings(sn)
                            await self.get_schedule_settings_ha(sn)
                            await self.get_scheduler(sn)
                            await self.compute_schedule(sn)

                if first and self.automatic:
                    await self.automatic_config()

                # Real time data every 5 minutes
                if first or (count_seconds % (5 * 60)) == 0:
                    for device in self.device_list:
                        sn = device.get("deviceSN", None)
                        if sn:
                            await self.get_real_time_data(sn)
                    await self.publish_data()

                if not self.api_started:
                    print("Fox API: Started")
                    self.api_started = True

                first = False

            except Exception as e:
                self.log("Error: Fox API: {}".format(e))
                self.log("Error: " + traceback.format_exc())

            await asyncio.sleep(1)
            count_seconds += 1

        print("Fox API: Stopped")

    async def stop(self):
        self.stop_api = True

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
        if result and isinstance(result, list):
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
                    {'unit': 'V', 'name': 'RVolt', 'variable': 'RVolt', 'value': 247.4}
                    ,{'unit': 'Hz', 'name': 'RFreq', 'variable': 'RFreq', 'value': 49.97}
                    ,{'unit': 'kW', 'name': 'RPower', 'variable': 'RPower', 'value': 7.993}
                    ,{'unit': '℃', 'name': 'AmbientTemperature', 'variable': 'ambientTemperation', 'value': 33.5}
                    ,{'unit': '℃', 'name': 'InvTemperation', 'variable': 'invTemperation', 'value': 29.7}
                    ,{'unit': '℃', 'name': 'batTemperature', 'variable': 'batTemperature', 'value': 33.6}
                    ,{'unit': 'kW', 'name': 'Load Power', 'variable': 'loadsPower', 'value': 17.046}
                    ,{'unit': 'kW', 'name': 'Output Power', 'variable': 'generationPower', 'value': 7.993}
                    ,{'unit': 'kW', 'name': 'Feed-in Power', 'variable': 'feedinPower', 'value': 0.0}
                    ,{'unit': 'kW', 'name': 'GridConsumption Power', 'variable': 'gridConsumptionPower', 'value': 9.053}
                    ,{'unit': 'V', 'name': 'InvBatVolt', 'variable': 'invBatVolt', 'value': 400.6}
                    ,{'unit': 'A', 'name': 'InvBatCurrent', 'variable': 'invBatCurrent', 'value': 16.4}
                    ,{'unit': 'kW', 'name': 'invBatPower', 'variable': 'invBatPower', 'value': 6.604}
                    ,{'unit': 'kW', 'name': 'Charge Power', 'variable': 'batChargePower', 'value': 0.0}
                    ,{'unit': 'kW', 'name': 'Discharge Power', 'variable': 'batDischargePower', 'value': 6.604}
                    ,{'unit': 'V', 'name': 'BatVolt', 'variable': 'batVolt', 'value': 399.1}
                    ,{'unit': 'A', 'name': 'BatCurrent', 'variable': 'batCurrent', 'value': 3.9}
                    ,{'unit': 'kW', 'name': 'MeterPower', 'variable': 'meterPower', 'value': 9.053}
                    ,{'unit': 'kW', 'name': 'Meter2Power', 'variable': 'meterPower2', 'value': 0.0}
                    ,{'unit': '%', 'name': 'SoC', 'variable': 'SoC', 'value': 26.0}
                    ,{'unit': 'kWh', 'name': 'Cumulative power generation', 'variable': 'generation', 'value': 6133.3}
                    ,{'unit': '0.01kWh', 'name': 'Battery Residual Energy', 'variable': 'ResidualEnergy', 'value': 10.34}
                    ,{'name': 'Running State', 'variable': 'runningState', 'value': '163'}
                    ,{'name': 'Battery Status', 'variable': 'batStatus', 'value': '1'}
                    ,{'name': 'Battery Status Name', 'variable': 'batStatusV2', 'value': 'Charge'}
                    ,{'name': 'The current error code is reported', 'variable': 'currentFault', 'value': ''}
                    ,{'name': 'The number of errors', 'variable': 'currentFaultCount', 'value': '0'}
                    ,{'unit': 'kWh', 'name': 'Battery throughput', 'variable': 'energyThroughput', 'value': 2255.872}
                    ,{'unit': '%', 'name': 'SOH', 'variable': 'SOH', 'value': 99.0}
                    ,{'unit': 'kWh', 'name': 'Total grid electricity consumption', 'variable': 'gridConsumption', 'value': 1712.7}
                    ,{'unit': 'kWh', 'name': 'Load power consumption', 'variable': 'loads', 'value': 4012.8}
                    ,{'unit': 'kWh', 'name': 'The total energy of the feeder', 'variable': 'feedin', 'value': 3730.6}
                    ,{'unit': 'kWh', 'name': 'Total charge energy', 'variable': 'chargeEnergyToTal', 'value': 1061.0},
                    {'unit': 'kWh', 'name': 'Total discharge energy', 'variable': 'dischargeEnergyToTal', 'value': 1532.6}
                ],
                'time': '2025-09-14 18:43:09 BST+0100', 'deviceSN': '60KE8020479C034'}
        ]
        """
        GET_REAL_TIME_DATA = "/op/v1/device/real/query"
        query = {"lang": FOX_LANG, "sns": [deviceSN]}
        result = await self.request_get(GET_REAL_TIME_DATA, post=True, datain=query)
        if result and isinstance(result, list):
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
        if result and isinstance(result, list):
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
        if result:
            self.device_detail[deviceSN] = result

    async def get_device_settings(self, deviceSN):
        """
        Get device settings
        """
        for key in FOX_SETTINGS:
            await self.get_device_setting(deviceSN, key)

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
        result = await self.request_get(GET_BATTERY_CHARGING_TIME, datain={"sn": deviceSN}, post=False)
        if result:
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

        minSocOnGrid = self.device_settings.get(deviceSN, {}).get("MinSocOnGrid", {}).get("value", 10)
        MinSoc = self.device_settings.get(deviceSN, {}).get("MinSoc", {}).get("value", 10)

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
                        "minSocOnGrid": minSocOnGrid,
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
                        "minSocOnGrid": minSocOnGrid,
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
        if charge_group:
            self.local_schedule[deviceSN]["charge"] = {}
            self.local_schedule[deviceSN]["charge"]["start_time"] = "{:02d}:{:02d}:00".format(charge_group.get("startHour", 0), charge_group.get("startMinute", 0))
            self.local_schedule[deviceSN]["charge"]["end_time"] = "{:02d}:{:02d}:00".format(charge_group.get("endHour", 0), charge_group.get("endMinute", 0))
            self.local_schedule[deviceSN]["charge"]["soc"] = charge_group.get("maxSoc", 100)
            self.local_schedule[deviceSN]["charge"]["power"] = self.fdpwr_max[deviceSN]
            self.local_schedule[deviceSN]["charge"]["enable"] = 1 if charge_group.get("enable", 0) else 0
        if discharge_group:
            self.local_schedule[deviceSN]["discharge"] = {}
            self.local_schedule[deviceSN]["discharge"]["start_time"] = "{:02d}:{:02d}:00".format(discharge_group.get("startHour", 0), discharge_group.get("startMinute", 0))
            self.local_schedule[deviceSN]["discharge"]["end_time"] = "{:02d}:{:02d}:00".format(discharge_group.get("endHour", 0), discharge_group.get("endMinute", 0))
            self.local_schedule[deviceSN]["discharge"]["soc"] = discharge_group.get("fdSoc", 100)
            self.local_schedule[deviceSN]["discharge"]["power"] = int(discharge_group.get("fdPwr", 0))
            self.local_schedule[deviceSN]["discharge"]["enable"] = 1 if discharge_group.get("enable", 0) else 0
        return battery_slots

    async def get_device_production(self, deviceSN):
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
        year = datetime.now().year
        variables = ["generation", "feedin", "gridConsumption", "chargeEnergyToTal", "dischargeEnergyToTal"]
        result = await self.request_get(GET_DEVICE_PRODUCTION, datain={"sn": deviceSN, "year": year, "dimension": "year", "variables": variables}, post=True)
        if result:
            self.device_production[deviceSN] = result

    async def get_device_power_generation(self, deviceSN):
        """
        {'month': 867.5999999999995, 'today': 17.699999999999818, 'cumulative': 5765.7}
        """
        GET_DEVICE_POWER = "/op/v0/device/generation"
        result = await self.request_get(GET_DEVICE_POWER, datain={"sn": deviceSN})
        if result:
            self.device_power_generation[deviceSN] = result

    async def set_scheduler_enabled(self, deviceSN, enabled):
        """
        Set scheduler enabled/disabled
        """

        # Do change enable if not already modified
        if self.device_scheduler.get(deviceSN, {}).get("enable", None) == enabled:
            self.log("Fox: Debug: Scheduler for {} already set to enabled {}".format(deviceSN, enabled))
            return

        SET_SCHEDULER_ENABLED = "/op/v1/device/scheduler/set/flag"
        result = await self.request_get(SET_SCHEDULER_ENABLED, datain={"deviceSN": deviceSN, "enable": 1 if enabled else 0}, post=True)
        if result:
            if deviceSN not in self.device_scheduler:
                self.device_scheduler[deviceSN] = {}
            self.device_scheduler[deviceSN]["enable"] = enabled

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
            same = True
            if len(current_groups) != len(groups):
                same = False
            else:
                for i in range(0, len(groups)):
                    for key in groups[i]:
                        if groups[i][key] != current_groups[i].get(key, None):
                            same = False
                            break
                    if not same:
                        break

            self.log("Fox: Debug: Setting scheduler for {} same={} current_enable={} current_groups={} new_groups={}".format(deviceSN, same, current_enable, current_groups, groups))
            if not same:
                result = await self.request_get(SET_SCHEDULER, datain={"deviceSN": deviceSN, "groups": groups}, post=True)
                if result:
                    self.device_scheduler[deviceSN]["enable"] = True
                    self.device_scheduler[deviceSN]["groups"] = groups

    async def publish_schedule_settings_ha(self, deviceSN):
        """
        Publish the schedule settings to HA
        """
        local_schedule = self.local_schedule.get(deviceSN, {})
        for direction in ["charge", "discharge"]:
            for attribute in ["start_time", "end_time", "soc", "enable", "power", "write"]:
                entity_id_select = "select.predbat_fox_{}_battery_schedule_{}_{}".format(deviceSN.lower(), direction, attribute)
                entity_id_number = "number.predbat_fox_{}_battery_schedule_{}_{}".format(deviceSN.lower(), direction, attribute)
                entity_id_switch = "switch.predbat_fox_{}_battery_schedule_{}_{}".format(deviceSN.lower(), direction, attribute)

                if attribute in ["start_time", "end_time"]:
                    value = local_schedule.get(direction, {}).get(attribute, "00:00:00")
                    if value not in OPTIONS_TIME_FULL:
                        value = "00:00:00"
                    self.base.dashboard_item(
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
                        self.base.dashboard_item(
                            entity_id_number,
                            state=value,
                            attributes={"min": 10, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": "Fox {} Battery Schedule {} {}".format(deviceSN, direction.capitalize(), attribute.replace("_", " ").capitalize()), "icon": "mdi:gauge"},
                            app="fox",
                        )
                    elif attribute == "power":
                        max_power = self.fdpwr_max.get(deviceSN, 8000)
                        self.base.dashboard_item(
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
                    self.base.dashboard_item(
                        entity_id_switch,
                        state="on" if value else "off",
                        attributes={"friendly_name": "Fox {} Battery Schedule {} {}".format(deviceSN, direction.capitalize(), attribute.replace("_", " ").capitalize()), "icon": "mdi:check-circle-outline"},
                        app="fox",
                    )
                elif attribute == "write":
                    # Write button - always off
                    value = False
                    self.base.dashboard_item(
                        entity_id_switch,
                        state="on" if value else "off",
                        attributes={"friendly_name": "Fox {} Battery Schedule {} {}".format(deviceSN, direction.capitalize(), attribute.replace("_", " ").capitalize()), "icon": "mdi:check-circle-outline"},
                        app="fox",
                    )

    async def get_schedule_settings_ha(self, deviceSN):
        """
        Get the current schedule from HA database
        """
        if deviceSN not in self.local_schedule:
            self.local_schedule[deviceSN] = {}
        for direction in ["charge", "discharge"]:
            if direction not in self.local_schedule[deviceSN]:
                self.local_schedule[deviceSN][direction] = {}
            for attribute in ["start_time", "end_time", "soc", "enable", "power"]:
                entity_id_select = "select.predbat_fox_{}_battery_schedule_{}_{}".format(deviceSN.lower(), direction, attribute)
                entity_id_number = "number.predbat_fox_{}_battery_schedule_{}_{}".format(deviceSN.lower(), direction, attribute)
                entity_id_switch = "switch.predbat_fox_{}_battery_schedule_{}_{}".format(deviceSN.lower(), direction, attribute)

                if attribute in ["start_time", "end_time"]:
                    value = self.base.get_state_wrapper(entity_id_select, default="00:00:00")
                    self.local_schedule[deviceSN][attribute] = value
                elif attribute in ["soc", "power"]:
                    default_value = 0
                    if attribute == "soc" and direction == "charge":
                        default_value = 100
                    elif attribute == "soc" and direction == "discharge":
                        default_value = self.fdsoc_min.get(deviceSN, 10)
                    elif attribute == "power":
                        default_value = self.fdpwr_max.get(deviceSN, 8000)
                    value = self.base.get_state_wrapper(entity_id_number, default=default_value)
                    try:
                        value = int(float(value))
                    except ValueError:
                        value = 0
                    self.local_schedule[deviceSN][attribute] = value
                elif attribute == "enable":
                    value = self.base.get_state_wrapper(entity_id_switch, default="off")
                    self.local_schedule[deviceSN][attribute] = 1 if value == "on" else 0

    async def get_scheduler(self, deviceSN):
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
        result = await self.request_get(GET_SCHEDULER, datain={"deviceSN": deviceSN}, post=True)
        if result:
            self.fdpwr_max[deviceSN] = result.get("properties", {}).get("fdpwr", {}).get("range", {}).get("max", 8000)
            self.fdsoc_min[deviceSN] = result.get("properties", {}).get("fdsoc", {}).get("range", {}).get("min", 10)
            self.log("Fox: Fetched schedule got {}".format(result))
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
        if result:
            devices = result.get("data", [])
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
        self.log("Fox: API Requesting {} {}".format("POST" if post else "GET", path))
        while retries < FOX_RETRIES:
            result, allow_retry = await self.request_get_func(path, post=post, datain=datain)
            if result is not None:
                return result
            if not allow_retry:
                break
            retries += 1
            await asyncio.sleep(retries * random.random())
        return result

    async def request_get_func(self, path, post=False, datain=None):
        headers = self.get_headers(path)
        url = FOX_DOMAIN + path
        self.log("Fox: API Request: path {} post {} datain {}".format(path, post, datain))
        try:
            if post:
                if datain:
                    response = await asyncio.to_thread(requests.post, url, headers=headers, json=datain, timeout=TIMEOUT)
                else:
                    response = await asyncio.to_thread(requests.post, url, headers=headers, timeout=TIMEOUT)
            else:
                response = await asyncio.to_thread(requests.get, url, headers=headers, params=datain, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            self.log(f"Warn: Fox: Exception during request to {url}: {e}")
            self.failures_total += 1
            return None, False

        status_code = response.status_code
        if status_code in [400, 401, 402, 403]:
            self.log("Warn: Fox: Authentication error with status code {} from {}".format(status_code, url))
            self.failures_total += 1
            return None, False

        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
            self.log("Warn: Fox: Failed to decode response from {} code {}".format(url, status_code))
            data = None
        except (requests.Timeout, requests.exceptions.ReadTimeout):
            self.log("Warn: Fox: Timeout from {}".format(url))
            return None, True
        except (requests.exceptions.RequestException, requests.exceptions.ConnectionError) as e:
            self.log("Warn: Fox: Could not connect to {}".format(url))
            return None, True

        if response.status_code in [200, 201]:
            if data is None:
                data = {}
            errno = data.get("errno", 0)
            msg = data.get("msg", "")
            if errno != 0:
                self.failures_total += 1
                if errno in [40400, 41200, 41203, 41935]:
                    # Rate limiting so wait up to 10 seconds
                    self.log("Info: Fox: Rate limiting detected, waiting...")
                    await asyncio.sleep(random.random() * 30 + 1)
                    return None, True
                elif errno in [40402]:
                    # Out of API calls for today
                    self.log("Warn: Fox: Has run out of API calls for today, sleeping...")
                    await asyncio.sleep(5 * 60)
                    return None, False
                elif errno in [44096]:
                    # Unsupported function code
                    self.log("Warn: Fox: Unsupported function code {} from {}".format(errno, url))
                    return None, False
                elif errno in [40257]:
                    # Invalid parameter
                    self.log("Warn: Fox: Invalid parameter {} from {} message {}".format(errno, url, msg))
                    return None, False
                else:
                    self.log("Warn: Fox: Error {} from {} message {}".format(errno, url, msg))
                return None, False

            if "result" in data:
                data = data["result"]
                if data is None:
                    data = {}

            self.last_success_timestamp = datetime.now(timezone.utc)
            return data, False
        else:
            self.failures_total += 1
            if response.status_code == 429:
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
        entity_name_sensor = "sensor.predbat_fox"
        entity_name_number = "number.predbat_fox"
        entity_name_select = "select.predbat_fox"
        entity_name_switch = "switch.predbat_fox"
        entity_name_binary_sensor = "binary_sensor.predbat_fox"
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

            self.base.dashboard_item(
                entity_name_sensor + "_" + sn.lower() + "_info",
                state=stationName,
                attributes={"friendly_name": f"Fox {sn} Info", "hasPV": hasPV, "hasBattery": hasBattery, "inverterCapacity": capacity, "batteryCapacity": battery_capacity, "hasScheduler": hasScheduler, "deviceType": deviceType, "stationName": stationName},
                app="fox",
            )
            if not hasBattery:
                capacity = 0
            self.base.dashboard_item(entity_name_sensor + "_" + sn.lower() + "_inverter_capacity", state=capacity, attributes={"friendly_name": f"Fox {sn} Inverter Capacity", "unit_of_measurement": "W"}, app="fox")
            self.base.dashboard_item(entity_name_sensor + "_" + sn.lower() + "_battery_capacity", state=battery_capacity / 1000.0, attributes={"friendly_name": f"Fox {sn} Battery Capacity", "unit_of_measurement": "kWh"}, app="fox")

            battery_rate_max = int(self.fdpwr_max.get(sn, 8000))
            self.base.dashboard_item(entity_name_sensor + "_" + sn.lower() + "_battery_rate_max", state=battery_rate_max, attributes={"friendly_name": f"Fox {sn} Battery Max Rate", "unit_of_measurement": "W"}, app="fox")

            reserve = int(self.fdsoc_min.get(sn, 10))
            self.base.dashboard_item(entity_name_sensor + "_" + sn.lower() + "_battery_reserve_min", state=reserve, attributes={"friendly_name": f"Fox {sn} Battery Reserve Min", "unit_of_measurement": "%"}, app="fox")

        for sn in self.device_values:
            for item_name in self.device_values[sn]:
                item = self.device_values[sn][item_name]
                state = item.get("value", None)
                name = item.get("name", item_name)
                units = item.get("unit", "")
                attributes = {
                    "unit_of_measurement": units,
                    "friendly_name": f"Fox {sn} {name}",
                }
                # Set device and state class
                if units in ['kWh', 'kW', 'W', 'W']:
                    attributes["device_class"] = "energy"
                entity_id = entity_name_sensor + "_" + sn.lower() + "_" + item_name.lower()
                if item_name.lower() in ["generation", "energythroughput", "gridconsumption", "loads", "feedin", "chargeenergytotal", "dischargeenergytotal"]:
                    attributes["state_class"] = "total_increasing"
                self.base.dashboard_item(entity_id, state=state, attributes=attributes, app="fox")

            # Publish schedule settings
            await self.publish_schedule_settings_ha(sn)

        for sn in self.device_settings:
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
                self.base.dashboard_item(entity_id, state=state, attributes=attributes, app="fox")

    async def write_setting_from_event(self, entity_id, value, is_number=False):
        """
        Handle write events
        """
        entity_id = entity_id.replace("number.predbat_fox_", "")
        entity_id = entity_id.replace("select.predbat_fox_", "")
        entity_id = entity_id.replace("switch.predbat_fox_", "")
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

        entity_id = entity_id.replace("switch.predbat_fox_", "")
        entity_id = entity_id.replace("select.predbat_fox_", "")
        entity_id = entity_id.replace("number.predbat_fox_", "")
        sn = entity_id.split("_")[0]
        serial = None
        for s in self.device_current_schedule:
            if s.lower() == sn.lower():
                serial = s
                break

        if not serial:
            self.log("Warn: Fox: Event, unknown serial number for {}: {}".format(entity_id, sn))
            return

        direction = ""
        direction = "charge" if "_charge_" in entity_id else direction
        direction = "discharge" if "_discharge_" in entity_id else direction
        if not direction:
            self.log("Warn: Fox: Event, unknown direction for {}: {}".format(entity_id, sn))
            return

        if serial not in self.local_schedule:
            self.local_schedule[serial] = {}
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
        for direction in ["charge", "discharge"]:
            enable = self.local_schedule[serial].get(direction, {}).get("enable", 0)
            if enable:
                start_time = self.local_schedule[serial].get(direction, {}).get("start_time", "00:00:00")
                end_time = self.local_schedule[serial].get(direction, {}).get("end_time", "00:00:00")
                soc = self.local_schedule[serial].get(direction, {}).get("soc", 100 if direction == "charge" else self.fdsoc_min.get(serial, 10))
                power = self.local_schedule[serial].get(direction, {}).get("power", self.fdpwr_max.get(serial, 8000))

                start_hour, start_minute = self.time_string_to_hour_minute(start_time, 0, 0)
                end_hour, end_minute = self.time_string_to_hour_minute(end_time, 0, 0)
                minSocOnGrid = self.device_settings.get(serial, {}).get("MinSocOnGrid", {}).get("value", 10)

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
                            "fdPwr": self.fdpwr_max.get(serial, 8000),
                            "minSocOnGrid": soc,
                        }
                    )
                elif direction == "discharge":
                    new_schedule.append(
                        {"enable": 1, "startHour": start_hour, "startMinute": start_minute, "endHour": end_hour, "endMinute": end_minute, "workMode": "ForceDischarge", "fdSoc": soc, "maxSoc": 100, "fdPwr": power, "minSocOnGrid": minSocOnGrid}
                    )

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
        for device in self.device_list:
            sn = device.get("deviceSN", None)
            detail = self.device_detail.get(sn, {})
            hasPV = detail.get("hasPV", False)
            hasBattery = detail.get("hasBattery", False)
            capacity = detail.get("capacity", 0) * 1000.0
            hasScheduler = detail.get("function", {}).get("scheduler", False)

            if hasBattery and hasScheduler and capacity > 0:
                batteries.append(sn.lower())
            if hasPV:
                pvs.append(sn.lower())

        num_inverters = len(batteries)
        self.log("Fox API: Found {} batteries and {} PVs".format(num_inverters, len(pvs)))
        if not num_inverters:
            raise ValueError("Fox API: No batteries with scheduler found, cannot configure")

        self.base.args["inverter_type"] = ["FoxCloud" for _ in range(num_inverters)]
        self.base.args["num_inverters"] = num_inverters
        self.base.args["inverter_mode"] = [f"select.predbat_fox_{device}_setting_workmode" for device in batteries]
        self.base.args["load_today"] = [f"sensor.predbat_fox_{device}_loads" for device in batteries]
        self.base.args["import_today"] = [f"sensor.predbat_fox_{device}_gridconsumption" for device in batteries]
        self.base.args["export_today"] = [f"sensor.predbat_fox_{device}_feedin" for device in batteries]
        self.base.args["pv_today"] = [f"sensor.predbat_fox_{device}_generation" for device in pvs]
        self.base.args["battery_rate_max"] = [f"sensor.predbat_fox_{device}_battery_rate_max" for device in batteries]
        self.base.args["battery_power"] = [f"sensor.predbat_fox_{device}_invbatpower" for device in batteries]
        self.base.args["grid_power"] = [f"sensor.predbat_fox_{device}_gridconsumptionpower" for device in batteries]
        self.base.args["grid_power_invert"] = [True for device in batteries]
        self.base.args["pv_power"] = [f"sensor.predbat_fox_{device}_pvpower" for device in pvs]
        self.base.args["load_power"] = [f"sensor.predbat_fox_{device}_loadspower" for device in batteries]
        self.base.args["soc_percent"] = [f"sensor.predbat_fox_{device}_soc" for device in batteries]
        self.base.args["soc_max"] = [f"sensor.predbat_fox_{device}_battery_capacity" for device in batteries]
        self.base.args["reserve"] = [f"number.predbat_fox_{device}_setting_minsocongrid" for device in batteries]
        self.base.args["battery_min_soc"] = [f"sensor.predbat_fox_{device}_battery_reserve_min" for device in batteries]
        self.base.args["charge_start_time"] = [f"select.predbat_fox_{device}_battery_schedule_charge_start_time" for device in batteries]
        self.base.args["charge_end_time"] = [f"select.predbat_fox_{device}_battery_schedule_charge_end_time" for device in batteries]
        self.base.args["charge_limit"] = [f"number.predbat_fox_{device}_battery_schedule_charge_soc" for device in batteries]
        self.base.args["scheduled_charge_enable"] = [f"switch.predbat_fox_{device}_battery_schedule_charge_enable" for device in batteries]
        self.base.args["charge_rate"] = [f"number.predbat_fox_{device}_battery_schedule_charge_power" for device in batteries]
        self.base.args["scheduled_discharge_enable"] = [f"switch.predbat_fox_{device}_battery_schedule_discharge_enable" for device in batteries]
        self.base.args["discharge_target_soc"] = [f"number.predbat_fox_{device}_battery_schedule_discharge_soc" for device in batteries]
        self.base.args["discharge_start_time"] = [f"select.predbat_fox_{device}_battery_schedule_discharge_start_time" for device in batteries]
        self.base.args["discharge_end_time"] = [f"select.predbat_fox_{device}_battery_schedule_discharge_end_time" for device in batteries]
        self.base.args["discharge_rate"] = [f"number.predbat_fox_{device}_battery_schedule_discharge_power" for device in batteries]
        self.base.args["battery_temperature"] = [f"sensor.predbat_fox_{device}_battemperature" for device in batteries]
        self.base.args["inverter_limit"] = [f"sensor.predbat_fox_{device}_inverter_capacity" for device in batteries]
        self.base.args["export_limit"] = [f"number.predbat_fox_{device}_setting_exportlimit" for device in batteries]
        self.base.args["schedule_write_button"] = [f"switch.predbat_fox_{device}_battery_schedule_charge_write" for device in batteries]


class MockBase:
    """Mock base class for testing"""

    def __init__(self):
        pass

    def log(self, message):
        print(f"LOG: {message}")

    def dashboard_item(self, *args, **kwargs):
        print(f"DASHBOARD: {args}, {kwargs}")


async def test_fox_api(api_key):
    """
    Run a test
    """
    print(f"Testing Fox API with key: {api_key[:10]}...")

    # Create a mock base object
    mock_base = MockBase()

    sn = "60KE8020479C034"

    # Create FoxAPI instance with a lambda that returns the API key
    fox_api = FoxAPI(api_key, False, mock_base)
    # device_List = await fox_api.get_device_list()
    # print(f"Device List: {device_List}")
    # await fox_api.start()
    # res = await fox_api.get_device_settings(sn)
    # res = await fox_api.get_battery_charging_time(sn)
    # res = await fox_api.get_scheduler(sn)
    # res = await fox_api.compute_schedule(sn)
    # res = await fox_api.publish_data()
    res = await fox_api.set_device_setting(sn, "dummy", 42)
    print(res)

    """
    groups = res.get('groups', [])
    # {'endHour': 0, 'fdPwr': 0, 'minSocOnGrid': 10, 'workMode': 'Invalid', 'fdSoc': 10, 'enable': 0, 'startHour': 0, 'maxSoc': 100, 'startMinute': 0, 'endMinute': 0},
    new_slot = groups[0].copy()
    new_slot["enable"] = 1
    new_slot["workMode"] = "ForceCharge"
    new_slot["startHour"] = 23
    new_slot["startMinute"] = 30
    new_slot["endHour"] = 23
    new_slot["endMinute"] = 59
    new_slot["fdSoc"] = 100
    new_slot["fdPwr"] = 8000
    new_slot2 = groups[1].copy()
    new_slot2["enable"] = 1
    new_slot2["workMode"] = "ForceCharge"
    new_slot2["startHour"] = 0
    new_slot2["startMinute"] = 00
    new_slot2["endHour"] = 5
    new_slot2["endMinute"] = 29
    new_slot2["fdSoc"] = 100
    new_slot2["fdPwr"] = 8000

    print("Sending: {}".format([new_slot, new_slot2]))
    res = await fox_api.set_scheduler(sn, [new_slot, new_slot2])
    print(res)
    #await fox_api.start()
    """


def main():
    """
    Main function for command line execution
    """
    parser = argparse.ArgumentParser(description="Test Fox API")
    parser.add_argument("--api-key", required=True, help="Fox API key")

    args = parser.parse_args()
    key = args.api_key

    # Run the test
    asyncio.run(test_fox_api(key))


if __name__ == "__main__":
    main()
