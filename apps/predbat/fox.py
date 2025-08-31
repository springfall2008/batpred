# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Fox API Library
# -----------------------------------------------------------------------------

import asyncio
from datetime import datetime
from datetime import timedelta
import traceback
import time
import hashlib
import requests
import json
import sys
import argparse
import random

# Define TIME_FORMAT_HA locally to avoid dependency issues
TIME_FORMAT_HA = "%Y-%m-%dT%H:%M:%S%z"

BASE_TIME = datetime.strptime("00:00", "%H:%M")
OPTIONS_TIME = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M")) for minute in range(0, 24 * 60, 1)]

FOX_DOMAIN = "https://www.foxesscloud.com"
FOX_LANG = 'en'
TIMEOUT = 60
FOX_SETTINGS = ["ExportLimit", "MaxSoc", "GridCode", "WorkMode", "ExportLimitPower", "MinSoc", "MinSocOnGrid"]

# Dummy attribute table for testing
fox_attribute_table = {
    "mode": {}
}

class FoxAPI:
    """Fox API client."""

    def __init__(self, key, base):
        self.base = base
        self.log = base.log
        self.key = key
        self.api_started = False
        self.stop_api = False
        self.failures_total = 0
        self.device_list = []
        self.device_detail = {}
        self.device_power_generation = {}
        self.available_variables = {}
        self.device_values = {}
        self.device_settings = {}
        self.device_production = {}

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

    async def start(self):
        """
        Main run loop
        """

        first = True
        count_seconds = 0
        while not self.stop_api:
            try:
                if first or (count_seconds % (30*60) == 0):
                    await self.get_device_list()
                    if first:
                        self.log("Fox API: Found {} devices".format(len(self.device_list)))
                    for device in self.device_list:
                        sn = device.get('deviceSN', None)
                        if sn:
                            await self.get_device_detail(sn)
                            await self.get_device_settings(sn)

                if first or (count_seconds % (5*60)) == 0:
                    for device in self.device_list:
                        sn = device.get('deviceSN', None)
                        if sn:
                            #await self.get_device_power_generation(sn)
                            await self.get_device_history(sn)
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

        await self.client.close()
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
                    name = variable.get('name', {})
                    name = name.get(FOX_LANG, '')
                    variable['name'] = name
                    available_data[variable_id] = variable
            self.available_variables = available_data

    async def get_device_history(self, deviceSN):
        """
        Get device history
        """
        GET_DEVICE_HISTORY = "/op/v0/device/history/query"
        timestamp = round(time.time() * 1000)
        query = {'sn': deviceSN, 'begin': timestamp - 1000*60*60*1, 'end': timestamp}
        result = await self.request_get(GET_DEVICE_HISTORY, post=True, datain=query)
        if result and isinstance(result, list):
            for item in result:
                if 'datas' in item:
                    datas = item['datas']
                    for data_item in datas:
                        unit = data_item.get('unit', '')
                        name = data_item.get('name', '')
                        variable = data_item.get('variable', '')
                        history = data_item.get('data', [])
                        point = history[-1] if history else {}
                        timestamp = point.get('time', "")
                        value = point.get('value', None)
                        if timestamp and variable and value is not None:
                            if deviceSN not in self.device_values:
                                self.device_values[deviceSN] = {}
                            self.device_values[deviceSN][variable] = {'timestamp': timestamp, 'value': value, 'unit': unit, 'name': name}

    async def get_device_detail(self, deviceSN):
        """
        Get device information

        {
            'deviceType': 'KH8', 
            'masterVersion': '1.34', 
            'afciVersion': '', 
            'hasPV': True, 
            'deviceSN': 
            '60KE8020479C034', 
            'slaveVersion': '1.01', 
            'capacity': 8, 
            'hasBattery': True, 
            'function': {'scheduler': True},
 
            'hardwareVersion': '--', 
            'managerVersion': '1.28', 
            'stationName': '2 Dona Fold', 
            'moduleSN': '609W6EUF46MB519', 
            'batteryList': 
                [{'batterySN': '60EP01104APP050', 'model': 'EP11', 'type': 'bcu', 'version': '1.005'}, 
                 {'batterySN': '60EP01104APP050', 'model': 'EP11', 'type': 'bmu', 'version': '1.05', 'capacity': 10360}, 
                 {'batterySN': '60EP01104APP050', 'model': 'EP11', 'type': 'ivu', 'version': '0.00'}], 
            'productType': 'KH', 
            'stationID': '2958ff16-13a5-4ab9-957a-79e938f86a19', 
            'status': 1
        }
        """
        GET_DEVICE_INFO = f"/op/v0/device/detail"
        query = {'sn': deviceSN}
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
        result = await self.request_get(GET_DEVICE_SETTING, datain={'sn': deviceSN, 'key': key}, post=True)
        if result:
            if deviceSN not in self.device_settings:
                self.device_settings[deviceSN] = {}
            self.device_settings[deviceSN][key] = result
            return result
        return None

    async def set_device_setting(self, deviceSN, key, value):
        """
        Set device setting
        """
        SET_DEVICE_SETTING = "/op/v0/device/setting/set"
        result = await self.request_get(SET_DEVICE_SETTING, datain={'sn': deviceSN, 'key': key, 'value': value, 'lang': FOX_LANG}, post=True)
        if result is None:
            return False
        return True

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
        variables = ["generation","feedin","gridConsumption","chargeEnergyToTal","dischargeEnergyToTal"]
        result = await self.request_get(GET_DEVICE_PRODUCTION, datain={'sn': deviceSN, 'year': year, 'dimension': 'year', 'variables': variables}, post=True)
        if result:
            self.device_production[deviceSN] = result

    
    async def get_device_power_generation(self, deviceSN):
        """
        {'month': 867.5999999999995, 'today': 17.699999999999818, 'cumulative': 5765.7}
        """
        GET_DEVICE_POWER = "/op/v0/device/generation"
        result = await self.request_get(GET_DEVICE_POWER, datain={'sn': deviceSN})
        if result:
            self.device_power_generation[deviceSN] = result

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
        query = {'pageSize': 100, 'currentPage': 1}
        result = await self.request_get(GET_DEVICE_LIST, post=True, datain=query)
        if result:
            devices = result.get('data', [])
            self.device_list = devices


    def get_headers(self, path):
        headers = {}
        token = self.key
        lang = FOX_LANG
        timestamp = str(round(time.time() * 1000))
        headers['token'] = token
        headers['lang'] = lang
        headers['timestamp'] = timestamp
        signature = fr'{path}\r\n{token}\r\n{timestamp}'
        headers['signature'] = hashlib.md5(signature.encode('UTF-8')).hexdigest()
        return headers

    async def request_get(self, path,  post=False, datain=None):
        """
        Retry wrapper
        """
        retries = 0
        while retries < 5:
            result = await self.request_get_func(path, post=post, datain=datain)
            if result is not None:
                return result
            retries += 1
        return result

    async def request_get_func(self, path,  post=False, datain=None):
        headers = self.get_headers(path)
        url = FOX_DOMAIN + path
        print("Request: path {} post {} datain {} headers {}".format(path, post, datain, headers))
        try:
            if post:
                if datain:
                    response = await asyncio.to_thread(requests.post, url, headers=headers, json=datain, timeout=TIMEOUT)
                else:
                    response = await asyncio.to_thread(requests.post, url, headers=headers, timeout=TIMEOUT)
            else:
                response = await asyncio.to_thread(requests.get, url, headers=headers, params=datain, timeout=TIMEOUT)
        except requests.exceptions.RequestException as e:
            self.log(f"Warn: GECloud: Exception during request to {url}: {e}")
            self.failures_total += 1
            return None

        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
            self.log("Warn: GeCloud: Failed to decode response from {}".format(url))
            data = None
        except (requests.Timeout, requests.exceptions.ReadTimeout):
            self.log("Warn: GeCloud: Timeout from {}".format(url))
            data = None
        except (requests.exceptions.RequestException, requests.exceptions.ConnectionError) as e:
            self.log("Warn: GeCloud: Could not connect to {}".format(url))
            data = None

        if response.status_code in [200, 201]:
            if data is None:
                data = {}
            errno = data.get('errno', 0)
            msg = data.get('msg', '')
            if errno != 0:
                self.failures_total += 1
                if errno == 40400:
                    # Rate limiting so wait up to 10 seconds
                    self.log("Fox: Rate limiting detected, waiting...")
                    await asyncio.sleep(random.random() * 10 + 1)
                else:
                    self.log("Warn: Fox: Error {} from {} message {}".format(errno, url, msg))
                return None
            
            if 'result' in data:
                data = data['result']
                if data is None:
                    data = {}

            self.last_success_timestamp = time.time()
            return data
        else:
            self.failures_total += 1
            if response.status_code == 429:
                # Rate limiting so wait up to 30 seconds
                await asyncio.sleep(random.random() * 30)
        return None

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


        for sn in self.device_values:
            for item_name in self.device_values[sn]:
                item = self.device_values[sn][item_name]
                state = item.get('value', None)
                name = item.get('name', item_name)
                attributes = {
                    'unit_of_measurement': item.get('unit', ''),
                    'friendly_name': name,
                }
                entity_id = entity_name_sensor + "_" + sn.lower() + "_" + item_name.lower()
                self.base.dashboard_item(entity_id, state=state, attributes=attributes, app="fox")

        for sn in self.device_settings:
            for setting in self.device_settings[sn]:
                item = self.device_settings[sn][setting]
                state = item.get('value', None)
                unit = item.get('unit', '')
                range = item.get('range', {})
                precision = item.get('precision', 1)
                enumList = item.get('enumList', [])

                name = setting
                attributes = {
                    'unit_of_measurement': unit,
                    'friendly_name': name,
                }
                if enumList:
                    # Selector
                    attributes['options'] = enumList
                    entity_id = entity_name_select + "_" + sn.lower() + "_" + setting.lower()
                elif range:
                    # Number
                    attributes['min'] = range.get('min', 0)
                    attributes['max'] = range.get('max', 100)
                    attributes['step'] = precision
                    entity_id = entity_name_number + "_" + sn.lower() + "_" + setting.lower()
                else:
                    # Sensor
                    entity_id = entity_name_sensor + "_" + sn.lower() + "_" + setting.lower()
                self.base.dashboard_item(entity_id, state=state, attributes=attributes, app="fox")

    async def write_setting_from_event(self, entity_id, value, is_number=False):
        """
        Handle write events
        """
        entity_id = entity_id.replace("number.predbat_fox_", "")
        entity_id = entity_id.replace("select.predbat_fox_", "")
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
                step = self.device_settings[serial][register].get('precision', None)
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
            if await self.set_device_setting(sn, register, value):
                self.device_settings[serial][register]['value'] = value
        else:
            self.log("Warn: Fox: Unknown select event for {}".format(entity_id))
        await self.publish_data()

    async def select_event(self, entity_id, value):
        """
        Handle select events
        """
        await self.write_setting_from_event(entity_id, value)

    async def number_event(self, entity_id, value):
        await self.write_setting_from_event(entity_id, value, is_number=True)

    async def switch_event(self, entity_id, service):
        pass



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
    
    # Create FoxAPI instance with a lambda that returns the API key
    fox_api = FoxAPI(api_key, mock_base)    
    res = await fox_api.get_device_setting('60KE8020479C034', 'WorkMode')
    print(res)
    res =await fox_api.set_device_setting('60KE8020479C034', 'WorkMode', 'SelfUse')
    print(res)
    res = await fox_api.get_device_setting('60KE8020479C034', 'WorkMode')
    print(res)
    #await fox_api.start()


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

