# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import aiohttp
import asyncio
import json
import argparse
import traceback
from datetime import datetime, timezone, timedelta
from component_base import ComponentBase

SOLAX_TIMEOUT = 20
SOLAX_RETRIES = 5
SOLAX_COMMAND_RETRY_DELAY = 2.0
SOLAX_COMMAND_MAX_RETRIES = 8
SOLAX_REGIONS = {
    "eu": "openapi-eu.solaxcloud.com", # cspell:disable-line
    "us": "openapi-us.solaxcloud.com", # cspell:disable-line
    "cn": "openapi.solaxcloud.com", # cspell:disable-line
}

# Business type constants
BUSINESS_TYPE_RESIDENTIAL = 1
BUSINESS_TYPE_COMMERCIAL = 4

# Device type mapping (for businessType=4 Commercial & Industrial)
SOLAX_DEVICE_TYPE_INVERTER = 1
SOLAX_DEVICE_TYPE_BATTERY = 2
SOLAX_DEVICE_TYPE_METER = 3
SOLAX_DEVICE_TYPE_EV_CHARGER = 4
SOLAX_DEVICE_TYPES = {
    SOLAX_DEVICE_TYPE_INVERTER: "Inverter",
    SOLAX_DEVICE_TYPE_BATTERY: "Battery",
    SOLAX_DEVICE_TYPE_METER: "Meter",
    SOLAX_DEVICE_TYPE_EV_CHARGER: "EV Charger",
}

# Plant state mapping for Residential (businessType=1)
SOLAX_PLANT_STATE_RESIDENTIAL = {
    0: "Connecting",
    1: "Offline",
    2: "Online",
    # >1: Online (handled programmatically)
}

# Plant state mapping for Commercial & Industrial (businessType=4)
SOLAX_PLANT_STATE_COMMERCIAL = {
    0: "Offline",
    1: "Normal",
    2: "Failure",
    3: "Warning",
    4: "Connecting",
}

# Device status mapping for Inverter (deviceType=1)
SOLAX_INVERTER_STATUS = {
    100: "Waiting",
    101: "Self-check",
    102: "Normal",
    103: "Fault",
    104: "Permanent Fault Mode",
    105: "Update Mode",
    106: "EPS Check Mode",
    107: "EPS Mode",
    108: "Self Test",
    109: "Idle Mode",
    110: "Standby Mode",
    111: "Pv Wake Up Bat Mode",
    112: "Gen Check Mode",
    113: "Gen run Mode",
    114: "RSD Standby",
    130: "VPP mode",
    131: "TOU-Self use",
    132: "TOU-Charging",
    133: "TOU-Discharging",
    134: "TOU-Battery off",
    135: "TOU-Peak Shaving",
    136: "Normal Mode(Gen)",
    137: "Normal Mode(BAT-E)",
    138: "Normal Mode(BAT-H)",
    139: "EPS mode(BAT-H)",
    140: "Start Mode",
    141: "Normal Mode(R-1)",
    142: "Normal Mode(R-2)",
    143: "Normal Mode(R-3)",
    144: "Normal Mode(R-4)",
    145: "Normal Mode(R-5)",
    146: "Normal Mode(R-6)",
    147: "Normal Mode(R-7)",
    150: "Self Use",
    151: "Force Time Use",
    152: "Back Up Mode",
    153: "Feedin Priority",
    154: "Demand Mode",
    155: "ConstPowr Mode",
    160: "OpenAdr Mode",
    170: "STOP MODE",
    171: "DEBUG MODE",
    174: "Normal(Smart selfuse)",
    175: "Normal(Smart feedin)",
    176: "Normal(Smart Bat not discharge)",
    177: "Normal(WLV 0%)",
    1301: "Power Control Mode",
    1302: "Electric Quantity Target Control Mode",
    1303: "SOC Target Control Mode",
    1304: "Push Power -Positive/Negative Mode",
    1305: "Push Power - Zero Mode",
    1306: "Self-Consume -Charge/Discharge Mode",
    1307: "Self-Consume - Charge Only Mode",
    1308: "PV&BAT Individual Setting- Duration Mode",
    1309: "PV&BAT Individual Setting-Target SOC Mode",
}

# Battery status mapping for Residential (deviceType=2, businessType=1)
SOLAX_BATTERY_STATUS_RESIDENTIAL = {
    0: "Idle",
    1: "Work",
}

# Battery status mapping for Commercial & Industrial (deviceType=2, businessType=4)
SOLAX_BATTERY_STATUS_COMMERCIAL = {
    0: "Idle",
    1: "Standby",
    2: "Discharge Pre-Charge",
    3: "Charge-to-discharge pre-charge",
    4: "Discharging",
    5: "Discharging Fault",
    6: "Charge switching current limit",
    7: "Charge Self-Test",
    8: "Charge Pre-Charge",
    9: "Charging",
    10: "Charging Fault",
    11: "Power Off Status",
}

# EV Charger status mapping (deviceType=4)
SOLAX_EV_CHARGER_STATUS = {
    0: "Available",
    1: "Preparing",
    2: "Charging",
    3: "Finish",
    4: "Faulted",
    5: "Unavailable",
    6: "Reserved",
    7: "SuspendedEV",
    8: "SuspendedEVSE",
    9: "Update",
    10: "CardActivation",
    11: "StartDelay",
    12: "ChargPause",  # cspell:disable-line
    13: "Stopping",
}

# EV Charger working mode mapping (deviceType=4, deviceWorkingMode field)
SOLAX_EV_CHARGER_WORKING_MODE = {
    0: "STOP",
    1: "FAST",
    2: "ECO",
    3: "GREEN",
}

# Device command delivery result status mapping
SOLAX_COMMAND_STATUS_OFFLINE = 1
SOLAX_COMMAND_STATUS_FAILED = 2
SOLAX_COMMAND_STATUS_ISSUE_SUCCESS = 3
SOLAX_COMMAND_STATUS_EXECUTION_SUCCESS = 4
SOLAX_COMMAND_STATUS_EXECUTION_FAILED = 5
SOLAX_COMMAND_STATUS_TIMEOUT = 6

SOLAX_COMMAND_STATUS = {
    1: "Device Offline",
    2: "Command issuance failed",
    3: "Command issuance succeeded",
    4: "Device execution succeeded",
    5: "Device execution failed",
    6: "Execution timed out",
}

# API response code mapping
SOLAX_API_CODES = {
    10000: "Operation successful",
    10001: "Operation failed",
    11500: "System busy, please try again later",
    10200: "Operation abnormality, please see the specific message content for details",
    10400: "Request not authenticated",
    10401: "Username or password incorrect",
    10402: "Request access_token authentication failed",
    10403: "Interface has no access rights",
    10404: "Callback function not configured",
    10405: "The number of API calls has been used up",
    10406: "The API call rate has reached the upper limit, please try again later",
    10500: "User has no device data permission",
}

# Device model mapping for Residential (businessType=1)
SOLAX_DEVICE_MODEL_RESIDENTIAL = {
    1: {  # Inverter
        1: "X1-LX", 2: "X-Hybrid", 3: "X1-Hybrid-G3", 4: "X1-Boost/Air/Mini", 5: "X3-Hybrid-G1/G2", # cspell:disable-line
        6: "X3-20K/30K", 7: "X3-MIC/PRO", 8: "X1-Smart", 9: "X1-AC", 10: "A1-Hybrid", # cspell:disable-line
        11: "A1-FIT", 12: "A1", 13: "J1-ESS", 14: "X3-Hybrid-G4", 15: "X1-Hybrid-G4", # cspell:disable-line
        16: "X3-MIC/PRO-G2", 17: "X1-SPT", 18: "X1-Boost-G4", 19: "A1-HYB-G2", 20: "A1-AC-G2", # cspell:disable-line
        21: "A1-SMT-G2", 22: "X1-Mini-G4", 23: "X1-IES", 24: "X3-IES", 25: "X3-ULT", # cspell:disable-line
        26: "X1-SMART-G2", 27: "A1-Micro 1 in 1", 28: "X1-Micro 2 in 1", 29: "X1-Micro 4 in 1", # cspell:disable-line
        31: "X3-AELIO", 32: "X3-HYB-G4 PRO", 33: "X3-NEO-LV", 34: "X1-VAST", 35: "X3-IES-P", # cspell:disable-line
        36: "J3-ULT-LV-16.5K", 37: "J3-ULT-30K", 38: "J1-ESS-HB-2", 39: "C3-IES", 40: "X3-IES-A", # cspell:disable-line
        41: "X1-IES-A", 43: "X3-ULT-GLV", 44: "X1-MINI-G4 PLUS", 46: "X1-Reno-LV", 47: "A1-HYB-G3", # cspell:disable-line
        100: "X3-FTH", 101: "X3-MGA-G2", 102: "X1-Hybrid-LV", 103: "X1-Lite-LV", 104: "X3-GRAND-HV", # cspell:disable-line
        105: "X3-FORTH-PLUS", # cspell:disable-line
    },
    3: {  # Meter
        50: "Meter X", 176: "M1-40", 178: "M3-40", 179: "M3-40-Dual", 181: "M3-40-Wide", # cspell:disable-line
    },
    4: {  # EV Charger
        1: "X1/X3-EVC", 2: "X1/X3-EVC G1.1", 3: "X1/X3-HAC", 4: "J1-EVC", 5: "A1-HAC", 6: "C1/C3-HAC", # cspell:disable-line
    },
}

# Device model mapping for Commercial & Industrial (businessType=4)
SOLAX_DEVICE_MODEL_COMMERCIAL = {
    1: {  # Inverter
        1: "X3-AELIO", 2: "X3-TRENE-100KI", 3: "X3-TRENE-100K", 4: "X3-TRENE", 16: "X3-PRO G2", # cspell:disable-line
        31: "X3-AELIO", 42: "X3-AELIO", 100: "X3-FORTH", 101: "X3-MEGA G2", 104: "X3-GRAND", # cspell:disable-line
        105: "X3-FORTH PLUS", # cspell:disable-line
    },
    2: {  # Battery
        1: "TB-HR140", 2: "TB-HR522", 145: "TSYS-HS51", 163: "TR-HR140", # cspell:disable-line
    },
    3: {  # Meter
        0: "DTSU666-CT", 1: "DTSU666-CT", 2: "DTSU666-CT", 3: "DTSU666-CT", # cspell:disable-line
        4: "Wi-BR DTSU666-CT", 5: "Wi-BR DTSU666-CT", 6: "CT", 7: "DTSU666-CT", # cspell:disable-line
        8: "UMG 103-CBM", 9: "M3-40-Dual", 10: "M3-40", 11: "PRISMA-310A", # cspell:disable-line
    },
    4: {  # EV Charger
        8: "UMG 103-CBM", 9: "M3-40-Dual", 10: "M3-40", 11: "PRISMA-310A", # cspell:disable-line
        1: "X1/X3-EVC", 2: "X1/X3-EVC G1.1", 3: "X1/X3-HAC", 4: "J1-EVC", 5: "A1-HAC", 6: "C1/C3-HAC", # cspell:disable-line
    },
}

# Flag field mapping for Commercial & Industrial (businessType=4)
SOLAX_FLAG_COMMERCIAL = {
    None: "Not in parallel",
    0: "Master",
    1: "Slave",
}

BASE_TIME = datetime.strptime("00:00", "%H:%M")
OPTIONS_TIME = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M")) for minute in range(0, 24 * 60, 1)]
OPTIONS_TIME_FULL = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M") + ":00") for minute in range(0, 24 * 60, 1)]

"""
SolaX management strategy


On startup set workmode to selfuse with no charging and allow discharge from 00:00 to 23:59

Inside a Predbat charge window use SOC target control mode (soc_target_control_mode) to reach target SOC by end of window
Inside a Predbat export window use SOC target control mode (soc_target_control_mode) to reach minimum SOC by end of window
Outside a Predbat window use self_consume_mode to avoid charging from grid and only discharge to support self consumption
For freeze charge use self_consume_charge_only_mode to avoid discharging to support self consumption
For freeze export - Use feedin_priority_mode to export to the grid and avoid discharging

HA Entity controls

On startup read the current HA control values and store them as the local schedule
Events will be used to trigger updates to the controls

Charge start time
Charge end time
Timed charge enable
Charge target SOC

- If charge target SOC == Current SOC then assume freeze charge

Export start time
Export end time
Timed export enable
Export target SOC

- If export target SOC == 99% then assume freeze export
"""


def as_int(value, default=0):
    """
    Safely convert a value to int, with a default fallback
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class SolaxAPI(ComponentBase):
    """
    SolaX Cloud API component for Predbat
    Handles authentication and plant information retrieval
    """

    def initialize(self, client_id, client_secret, region="eu", plant_id=None, automatic=False, enable_controls=True, plant_sn=None):
        """
        Initialize the SolaX API component

        Args:
            client_id: SolaX Cloud client ID
            client_secret: SolaX Cloud client secret
            region: API region (eu, us, cn), defaults to eu
            plant_id: Optional specific plant ID to filter
            automatic: Enable automatic control
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.region = region
        self.plant_id = plant_id
        self.automatic = automatic
        self.current_mode_hash = None
        self.current_mode_hash_timestamp = None
        self.enable_controls = enable_controls

        # Build base URL from region
        self.base_url = f"https://{SOLAX_REGIONS.get(region, SOLAX_REGIONS['eu'])}"

        # Token storage (memory only)
        self.access_token = None
        self.token_expiry = None

        # Data storage
        self.plant_list = []
        self.plant_info = []
        self.plant_inverters = {}
        self.plant_batteries = {}
        self.device_info = {}
        self.realtime_data = {}
        self.realtime_device_data = {}
        self.controls = {}

        # Error tracking
        self.error_count = 0

        # Convert plant_sn to list
        if plant_sn is None:
            self.plant_sn_filter = []
        elif isinstance(plant_sn, str):
            self.plant_sn_filter = [plant_sn]
        else:
            self.plant_sn_filter = plant_sn

        self.log(f"SolaX API: Initialized with region={region}, base_url={self.base_url}")

    async def automatic_config(self):
        """
        Automatically configure the base args based on the plants and devices found
        """

        # Find all plants with inverters and batteries
        plants = []
        for plant_id in self.plant_inverters:
            inverter_sns = self.plant_inverters[plant_id]
            if inverter_sns:
                # Check if plant has at least one battery
                has_battery = False
                for device_sn in self.device_info:
                    device = self.device_info[device_sn]
                    if device.get("plantId") == plant_id and device.get("deviceType") == 2:  # Battery
                        has_battery = True
                        break


                if has_battery:
                    plants.append(plant_id)
                    self.log(f"SolaX API: Found plant {plant_id} with {len(inverter_sns)} inverter(s) and battery")

        num_inverters = len(plants)
        if not num_inverters:
            raise ValueError("SolaX API: No plants with inverters and batteries found, cannot configure")

        self.log(f"SolaX API: Configuring {num_inverters} plant(s) for Predbat")

        # Set basic inverter configuration
        self.set_arg("inverter_type", ["SolaxCloud" for _ in range(num_inverters)])
        self.set_arg("num_inverters", num_inverters)

        # Set up entity references for each plant
        # Load/import/export from plant realtime data
        self.set_arg("load_today", [f"sensor.{self.prefix}_solax_{plant}_total_load" for plant in plants])
        self.set_arg("import_today", [f"sensor.{self.prefix}_solax_{plant}_total_imported" for plant in plants])
        self.set_arg("export_today", [f"sensor.{self.prefix}_solax_{plant}_total_exported" for plant in plants])
        self.set_arg("pv_today", [f"sensor.{self.prefix}_solax_{plant}_total_yield" for plant in plants])
        self.set_arg("battery_power", [f"sensor.{self.prefix}_solax_{plant}_battery_charge_discharge_power" for plant in plants])

        # Power and SOC from device realtime data (using first inverter)
        inverter_list = [self.plant_inverters[plant][0] for plant in plants]
        self.set_arg("grid_power", [f"sensor.{self.prefix}_solax_{plant}_{inv}_grid_power" for plant, inv in zip(plants, inverter_list)])
        self.set_arg("pv_power", [f"sensor.{self.prefix}_solax_{plant}_{inv}_pv_power" for plant, inv in zip(plants, inverter_list)])
        self.set_arg("load_power", [f"sensor.{self.prefix}_solax_{plant}_{inv}_ac_power" for plant, inv in zip(plants, inverter_list)])

        # Sensors
        self.set_arg("battery_temperature", [f"sensor.{self.prefix}_solax_{plant}_battery_temperature" for plant in plants])
        self.set_arg("soc_max", [f"sensor.{self.prefix}_solax_{plant}_battery_capacity" for plant in plants])
        self.set_arg("soc_kw", [f"sensor.{self.prefix}_solax_{plant}_battery_soc" for plant in plants])
        self.set_arg("battery_rate_max_charge", [f"sensor.{self.prefix}_solax_{plant}_battery_max_power" for plant in plants])
        self.set_arg("inverter_limit", [f"sensor.{self.prefix}_solax_{plant}_inverter_max_power" for plant in plants])

        # Control entities using the controls system
        self.set_arg("reserve", [f"number.{self.prefix}_solax_{plant}_setting_reserve" for plant in plants])
        self.set_arg("charge_start_time", [f"select.{self.prefix}_solax_{plant}_battery_schedule_charge_start_time" for plant in plants])
        self.set_arg("charge_end_time", [f"select.{self.prefix}_solax_{plant}_battery_schedule_charge_end_time" for plant in plants])
        self.set_arg("charge_limit", [f"number.{self.prefix}_solax_{plant}_battery_schedule_charge_target_soc" for plant in plants])
        self.set_arg("scheduled_charge_enable", [f"switch.{self.prefix}_solax_{plant}_battery_schedule_charge_enable" for plant in plants])
        self.set_arg("charge_rate", [f"number.{self.prefix}_solax_{plant}_battery_schedule_charge_rate" for plant in plants])
        self.set_arg("scheduled_discharge_enable", [f"switch.{self.prefix}_solax_{plant}_battery_schedule_export_enable" for plant in plants])
        self.set_arg("discharge_target_soc", [f"number.{self.prefix}_solax_{plant}_battery_schedule_export_target_soc" for plant in plants])
        self.set_arg("discharge_start_time", [f"select.{self.prefix}_solax_{plant}_battery_schedule_export_start_time" for plant in plants])
        self.set_arg("discharge_end_time", [f"select.{self.prefix}_solax_{plant}_battery_schedule_export_end_time" for plant in plants])
        self.set_arg("discharge_rate", [f"number.{self.prefix}_solax_{plant}_battery_schedule_export_rate" for plant in plants])

        # Historical data (use first battery)
        if plants:
            self.set_arg("battery_temperature_history", f"sensor.{self.prefix}_solax_{plants[0]}_battery_temperature")

        self.log(f"SolaX API: Automatic configuration complete for {num_inverters} plant(s)")

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
            await self.write_setting_from_event(entity_id, value)
        elif "_battery_schedule_" in entity_id:
            await self.write_battery_schedule_event(entity_id, value)

    async def switch_event(self, entity_id, service):
        if "_battery_schedule_" in entity_id:
            await self.write_battery_schedule_event(entity_id, service)

    async def write_battery_schedule_event(self, entity_id, value):
        """
        Write a battery schedule based on an event

        Args:
            entity_id: Home Assistant entity ID
            value: New value
        """
        # Extract plant ID, direction, and field from entity_id
        # Entity format: {domain}.{prefix}_solax_{plant_id}_battery_schedule_{direction}_{field}
        parts = entity_id.split("_")
        try:
            solax_index = parts.index("solax")
            plant_id = parts[solax_index + 1]

            # Find direction and field
            if "charge" in parts:
                direction = "charge"
                charge_index = parts.index("charge")
                field = "_".join(parts[charge_index + 1:])
            elif "export" in parts:
                direction = "export"
                export_index = parts.index("export")
                field = "_".join(parts[export_index + 1:])
            else:
                self.log(f"SolaX API: Unable to parse direction from entity_id {entity_id}")
                return
        except (ValueError, IndexError):
            self.log(f"SolaX API: Unable to parse entity_id {entity_id}")
            return

        # Update controls dictionary
        if plant_id not in self.controls:
            self.log(f"Warn: SolaX API: No controls found for plant {plant_id}")
            return False
        if direction not in self.controls[plant_id]:
            self.log(f"Warn: SolaX API: No controls found for plant {plant_id} direction {direction}")
            return False

        if field == "enable":
            value = self.apply_service_to_toggle(self.controls[plant_id][direction].get(field, False), value)
        elif '_time' in field:
            # Ensure time is in HH:MM:SS format
            if len(value) == 5:
                value = value + ":00"
            if value not in OPTIONS_TIME_FULL:
                self.log(f"SolaX API: Invalid time value {value} for {entity_id}")
                return
        elif field in ['rate', 'target_soc']:
            try:
                value = int(value)
            except ValueError:
                self.log(f"SolaX API: Invalid number value {value} for {entity_id}")
                return
            item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self.control_info(plant_id, direction, field)
            value = max(min_value, min(max_value, value))

        self.controls[plant_id][direction][field] = value
        self.log(f"SolaX API: Updated battery schedule for plant {plant_id}, direction {direction}, field {field} to {value}")
        await self.publish_controls()

    async def write_setting_from_event(self, entity_id, value):
        """
        Write a control setting based on an event

        Example: number.predbat_solax_1618699116555534337_battery_schedule_charge_rate

        Args:
            entity_id: Home Assistant entity ID
            value: New value


        """
        # Extract plant ID, direction, and field from entity_id
        # Entity format: {domain}.{prefix}_solax_{plant_id}_setting_{field}
        parts = entity_id.split("_")
        try:
            solax_index = parts.index("solax")
            plant_id = parts[solax_index + 1]
            field = "_".join(parts[solax_index + 3:])
        except (ValueError, IndexError):
            self.log(f"SolaX API: Unable to parse entity_id {entity_id}")
            return

        # Update controls dictionary
        if plant_id not in self.controls:
            self.log(f"Warn: SolaX API: No controls found for plant {plant_id}")
            return False

        item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self.control_info(plant_id, None, field)

        if field_type == 'number':
            try:
                value = int(value)
            except ValueError:
                self.log(f"SolaX API: Invalid number value {value} for {entity_id}")
                return
        self.controls[plant_id][field] = value
        self.log(f"SolaX API: Updated control for plant {plant_id}, field {field} to {value}")
        await self.publish_controls()
        return True

    def get_max_power_inverter(self, plant_id):
        rated_power = 0
        for device_id in self.plant_inverters.get(plant_id, []):
            try:
                rated_power += float(self.device_info.get(device_id, {}).get("ratedPower", 0))  # in kW
            except (TypeError, ValueError):
                pass
        return rated_power * 1000  # Convert to Watts

    def get_max_power_battery(self, plant_id):
        rated_power = 0
        for device_id in self.plant_batteries.get(plant_id, []):
            try:
                rated_power += float(self.device_info.get(device_id, {}).get("ratedPower", 0))  # in kW
            except (TypeError, ValueError):
                pass
        if rated_power == 0:
            # Fallback to inverter power if no battery power found
            rated_power = self.get_max_power_inverter(plant_id) / 1000  # Convert back to kW
        return rated_power * 1000  # Convert to Watts

    def get_max_soc_battery(self, plant_id):
        max_soc = 0
        for plant_info in self.plant_info:
            if plant_info.get("plantId") == plant_id:
                try:
                    max_soc += plant_info.get("batteryCapacity", 0)
                except (TypeError, ValueError):
                    pass
        ## Fallback to calculating from current SOC if not available
        if max_soc == 0:
            battery_soc, max_soc = self.get_current_soc_battery_kwh(plant_id)
        return max_soc  # in kWh

    def get_charge_discharge_power_battery(self, plant_id):
        total_power = 0
        for device_id in self.plant_batteries.get(plant_id, []):
            total_power += self.realtime_device_data.get(device_id, {}).get("chargeDischargePower", 0)
        return total_power  # in Watts

    def get_current_soc_battery_kwh(self, plant_id):
        current_soc = 0
        count_devices = 0
        battery_remainings = 0
        battery_size_max = 0
        for device_id in self.plant_batteries.get(plant_id, []):
            current_soc += self.realtime_device_data.get(device_id, {}).get("batterySOC", 0)
            battery_remainings += self.realtime_device_data.get(device_id, {}).get("batteryRemainings", 0)
            count_devices += 1
        if count_devices > 0:
            current_soc = current_soc / count_devices
            battery_size_max = round(battery_remainings * 100 / current_soc if current_soc > 0 else 0, 2)
        return battery_remainings, battery_size_max  # in kWh

    def get_battery_temperature(self, plant_id):
        temperature = 100.0
        for device_id in self.plant_batteries.get(plant_id, []):
            temperature = min(self.realtime_device_data.get(device_id, {}).get("batteryTemperature", temperature), temperature)
        if temperature == 100.0:
            temperature = None
        return temperature

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


    async def apply_controls(self, plant_id):
        """
        Apply control settings to the plant

        Args:
            plant_id: Plant ID
        """
        if plant_id not in self.controls:
            self.log(f"Warn: SolaX API: No controls found for plant {plant_id}")
            return False

        # 1. Work out if the current time is inside a charge or export window
        now = datetime.now(self.local_tz)
        rated_power = self.get_max_power_battery(plant_id)
        sn_list = self.plant_inverters.get(plant_id, [])
        if not sn_list:
            self.log(f"Warn: SolaX API: No inverters found for plant {plant_id}")
            return False

        charge_window = False
        export_window = False

        current_soc_kwh, max_soc_kwh = self.get_current_soc_battery_kwh(plant_id)
        current_soc = int((current_soc_kwh / max_soc_kwh) * 100) if max_soc_kwh > 0 else 0
        reserve_soc = as_int(self.controls.get(plant_id, {}).get("reserve"), 10)
        charge_start_str = self.controls.get(plant_id, {}).get("charge", {}).get("start_time", "00:00:00")
        charge_end_str = self.controls.get(plant_id, {}).get("charge", {}).get("end_time", "00:00:00")
        charge_enable = self.controls.get(plant_id, {}).get("charge", {}).get("enable", False)
        charge_target_soc = as_int(self.controls.get(plant_id, {}).get("charge", {}).get("target_soc"), 100)
        charge_power = as_int(self.controls.get(plant_id, {}).get("charge", {}).get("rate"), rated_power)
        export_start_str = self.controls.get(plant_id, {}).get("export", {}).get("start_time", "00:00:00")
        export_end_str = self.controls.get(plant_id, {}).get("export", {}).get("end_time", "00:00:00")
        export_enable = self.controls.get(plant_id, {}).get("export", {}).get("enable", False)
        export_target_soc = as_int(self.controls.get(plant_id, {}).get("export", {}).get("target_soc"), 10)
        export_power = as_int(self.controls.get(plant_id, {}).get("export", {}).get("rate"), rated_power)

        if charge_enable:
            charge_start = now.replace(hour=int(charge_start_str.split(":")[0]), minute=int(charge_start_str.split(":")[1]), second=0, microsecond=0)
            charge_end = now.replace(hour=int(charge_end_str.split(":")[0]), minute=int(charge_end_str.split(":")[1]), second=0, microsecond=0)
            charge_end_minutes = charge_end.hour * 60 + charge_end.minute
            if charge_end <= charge_start:
                charge_end += timedelta(days=1)
            if charge_start <= now <= charge_end:
                charge_window = True
        if export_enable:
            export_start = now.replace(hour=int(export_start_str.split(":")[0]), minute=int(export_start_str.split(":")[1]), second=0, microsecond=0)
            export_end = now.replace(hour=int(export_end_str.split(":")[0]), minute=int(export_end_str.split(":")[1]), second=0, microsecond=0)
            export_end_minutes = export_end.hour * 60 + export_end.minute
            if export_end <= export_start:
                export_end += timedelta(days=1)
            if export_start <= now <= export_end:
                export_window = True

        # Export takes priority over charge (although overlapping windows should not be possible)
        if export_window:
            new_target_soc = max(export_target_soc, reserve_soc)
            duration = (export_end - now).total_seconds()
            new_end = export_end_minutes
            if new_target_soc >= current_soc:
                # Freeze export
                new_mode = "freeze_export"
                new_power = 0
                new_target_soc = current_soc
            else:
                new_mode = "export"
                new_power = -export_power
        elif charge_window:
            duration = (charge_end - now).total_seconds()
            new_end = charge_end_minutes
            new_target_soc = max(charge_target_soc, reserve_soc)
            if (new_target_soc == reserve_soc) or (new_target_soc == current_soc):
                # Freeze charge
                new_mode = "freeze_charge"
                new_power = 0
                new_target_soc = current_soc
            elif new_target_soc < current_soc:
                # Target SOC is lower than current, go to ECO mode
                new_mode = "eco"
                new_power = 0
            else:
                new_mode = "charge"
                new_power = charge_power
        else:
            new_mode = "eco"
            new_target_soc = reserve_soc
            new_power = 0
            duration = 12 * 60 * 60  # Default to 12 hours
            new_end = 0

        duration = min(duration, 12 * 60 * 60)  # Max duration 12 hours
        new_mode_hash = hash((new_mode, new_power, new_target_soc, new_end))
        old_mode_hash = self.current_mode_hash
        old_mode_hash_timestamp = self.current_mode_hash_timestamp
        # Check age of current mode hash
        if old_mode_hash_timestamp:
            age = (now - old_mode_hash_timestamp).total_seconds()
            if age > 15 * 60:
                old_mode_hash = None  # Force update if older than 15 minutes
        if old_mode_hash is not None and new_mode_hash == old_mode_hash:
            self.log(f"SolaX API: No control changes for plant {plant_id}, skipping")
        else:
            success = True
            if new_mode == "eco":
                self.log(f"SolaX API: Plant {plant_id} : {sn_list} Applying self consume mode")
                success1 = await self.set_default_work_mode(sn_list, mode="selfuse")
                success2 = await self.self_consume_mode(sn_list, time_of_duration=duration)
                success = success1 and success2
            elif new_mode == "freeze_charge":
                self.log(f"SolaX API: Plant {plant_id} : {sn_list} Applying self consume charge only mode")
                success2 = await self.set_default_work_mode(sn_list, mode="selfuse")
                success1 = await self.self_consume_charge_only_mode(sn_list, time_of_duration=duration)
                success = success1 and success2
            elif new_mode == "freeze_export":
                success1 = await self.set_default_work_mode(sn_list, mode="feedin")
                success2 = await self.exit_vpp_mode(sn_list)
                success = success1 and success2
            elif new_mode == "charge":
                self.log(f"SolaX API: Plant {plant_id} : {sn_list} Applying SOC target control mode")
                success1 = await self.set_default_work_mode(sn_list, mode="selfuse")
                success2 = await self.soc_target_control_mode(sn_list, new_target_soc, charge_discharge_power=new_power)
                success = success1 and success2
            elif new_mode == "export":
                self.log(f"SolaX API: Plant {plant_id} : {sn_list} Applying SOC target control mode for export")
                success1 = await self.set_default_work_mode(sn_list, mode="feedin")
                success2 = await self.soc_target_control_mode(sn_list, new_target_soc, charge_discharge_power=new_power)
                success = success1 and success2
            else:
                self.log(f"SolaX API: Unknown mode {new_mode} for plant {plant_id}")
                success = False

            if success:
                self.current_mode_hash = new_mode_hash
                self.current_mode_hash_timestamp = now
                self.log(f"SolaX API: Applied new mode {new_mode} target_soc {new_target_soc} new_power {new_power} for plant {plant_id}")

        self.log(f"SolaX API: Applied controls for plant {plant_id}")
        return True

    def control_info(self, plant_id, direction, field):
        """
        Get control settings for a specific plant

        Args:
            plant_id: Plant ID
            direction: "charge" or "export" or None
            field: Control field (start_time, end_time, enable, target_soc)
        Returns:
            item_name, ha_name, friendly_name, field_type, default
        """
        field_type = 'select'
        if direction is None:
            item_name = f"solax_{plant_id}_setting_{field}"
            friendly_name = f"SolaX {plant_id} {field.replace('_', ' ').capitalize()}"
        else:
            item_name = f"solax_{plant_id}_battery_schedule_{direction}_{field}"
            friendly_name = f"SolaX {plant_id} {direction.capitalize()} {field.replace('_', ' ').capitalize()}"
        default = None
        field_units = None
        min_value = None
        max_value = None
        if '_time' in field:
            default = "00:00"
            field_type = 'select'
            field_units = "time"
        elif field == "enable":
            default = False
            field_type = 'switch'
        elif field == "target_soc":
            field_type = 'number'
            field_units = "%"
            min_value = 10
            max_value = 100
            if direction == "charge":
                default = max_value
            else:
                default = min_value
        elif field == "rate":
            max_value = self.get_max_power_inverter(plant_id)
            min_value = 0
            default = max_value
            field_type = 'number'
            field_units = "W"
        elif field == "reserve":
            min_value = 10
            max_value = 100
            default = min_value
            field_type = 'number'
            field_units = "%"
        ha_name = field_type + '.' + self.prefix + "_" + item_name
        return item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value

    async def fetch_controls(self, plant_id):
        """
        Fetch control settings using get_state_wrapper
        """
        for direction in ["charge", "export"]:
            for field in ["start_time", "end_time", "enable", "target_soc", "rate"]:
                item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self.control_info(plant_id, direction, field)
                state = self.get_state_wrapper(ha_name, default=default)
                if plant_id not in self.controls:
                    self.controls[plant_id] = {}
                if direction not in self.controls[plant_id]:
                    self.controls[plant_id][direction] = {}
                if field_type == 'number':
                    state = as_int(state, default=default)
                    state = max(min_value, min(max_value, state))
                self.controls[plant_id][direction][field] = state
        for field in ["reserve"]:
            item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self.control_info(plant_id, None, field)
            state = self.get_state_wrapper(ha_name, default=default)
            if field_type == 'number':
                state = as_int(state, default=default)
                state = max(min_value, min(max_value, state))
            if plant_id not in self.controls:
                self.controls[plant_id] = {}
            self.controls[plant_id][field] = state

        return True

    async def publish_controls(self):
        """
        Publish controls to dashboard items
        """

        for plant_id in self.controls:
            for direction in ["charge", "export"]:
                for field in ["start_time", "end_time", "enable", "target_soc", "rate"]:
                    item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self.control_info(plant_id, direction, field)
                    value = self.controls.get(plant_id, {}).get(direction, {}).get(field, default)
                    ha_name = field_type + '.' + self.prefix + "_" + item_name
                    attributes = {
                        "friendly_name": friendly_name
                    }
                    if field_units is not None:
                        attributes["unit_of_measurement"] = field_units
                    if min_value is not None:
                        attributes["min"] = min_value
                    if max_value is not None:
                        attributes["max"] = max_value
                        attributes["step"] = 1
                    if '_time' in field:
                        attributes["options"] = OPTIONS_TIME_FULL
                    self.dashboard_item(
                        ha_name,
                        state=value,
                        attributes=attributes,
                        app="solax",
                    )
            for field in ["reserve"]:
                    item_name, ha_name, friendly_name, field_type, field_units, default, min_value, max_value = self.control_info(plant_id, None, field)
                    value = self.controls.get(plant_id, {}).get(field, default)
                    ha_name = field_type + '.' + self.prefix + "_" + item_name
                    self.dashboard_item(
                        ha_name,
                        state=value,
                        attributes={
                            "friendly_name": friendly_name,
                            "unit_of_measurement": field_units,
                            "min": min_value,
                            "max": max_value,
                            "step": 1,
                        },
                        app="solax",
                    )


    def decode_api_code(self, code):
        """
        Decode API response code

        Args:
            code: API response code

        Returns:
            tuple: (is_error, description)
        """
        description = SOLAX_API_CODES.get(code, f"Unknown error code: {code}")
        is_error = code != 10000
        return is_error, description

    async def get_access_token(self):
        """
        Obtain access token from SolaX Cloud API

        Returns:
            Token string on success, None on failure
        """
        url = f"{self.base_url}/openapi/auth/get_token"
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "CICS", # cspell:disable-line
        }

        try:
            timeout = aiohttp.ClientTimeout(total=SOLAX_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as response:
                    status = response.status
                    try:
                        data = await response.json()
                    except (aiohttp.ContentTypeError, json.JSONDecodeError) as e:
                        self.log(f"Warn: SolaX API: Failed to decode auth response: {e}")
                        self.error_count += 1
                        return None

                    if status != 200:
                        self.log(f"Warn: SolaX API: Auth request failed with status {status}")
                        self.error_count += 1
                        return None

                    code = data.get("code")
                    if code == 10402:
                        self.log("Warn: SolaX API: Auth failed - Invalid client ID or secret")
                        self.token_expiry = None
                        self.access_token = None
                        # Don't count error here as its counted when the exception is raised caught
                        raise aiohttp.ClientError("Invalid client ID or secret")
                    elif code != 0:
                        error_msg = data.get("message", "Unknown error")
                        self.log(f"Warn: SolaX API: Auth failed with code {data.get('code')}: {error_msg}")
                        self.error_count += 1
                        return None

                    result = data.get("result", {})
                    access_token = result.get("access_token")
                    expires_in = result.get("expires_in", 2591999)  # Default ~30 days

                    if not access_token:
                        self.log("Warn: SolaX API: No access token in response")
                        self.error_count += 1
                        return None

                    self.access_token = access_token
                    self.token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                    self.log(f"SolaX API: Successfully obtained access token, expires at {self.token_expiry.isoformat()}")
                    return access_token

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: SolaX API: Exception during authentication: {e}")
            self.error_count += 1
            return None

    async def request_wrapper(self, func):
        """
        Wrapper to retry requests up to SOLAX_RETRIES times

        Args:
            func: Async function to call

        Returns:
            Result from func on success, None on failure
        """
        for retry in range(SOLAX_RETRIES):
            try:
                result = await func()
                return result
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if retry < SOLAX_RETRIES - 1:
                    self.log(f"Warn: SolaX API: Request failed (attempt {retry + 1}/{SOLAX_RETRIES}): {e}")
                    await asyncio.sleep(retry * 0.5)
                else:
                    self.log(f"Warn: SolaX API: Request failed after {SOLAX_RETRIES} attempts: {e}")
                    self.error_count += 1
            except Exception as e:
                self.log(f"Warn: SolaX API: Unexpected exception during request: {e}\n{traceback.format_exc()}")
                self.error_count += 1
                break
        return None

    async def _request_get_impl(self, path, params=None, post=False, json_data=None):
        """
        Internal implementation of GET/POST request

        Args:
            path: API path (e.g., /openapi/v2/plant/page_plant_info)
            params: URL parameters dict
            post: If True, use POST method
            json_data: JSON data for POST requests

        Returns:
            Parsed JSON response or None on failure
        """
        # Check if token needs refresh
        if self.access_token is None or self.token_expiry is None or self.token_expiry < datetime.now(timezone.utc):
            self.log("SolaX API: Token expired or missing, refreshing...")
            token = await self.get_access_token()
            if not token:
                return None

        # Build full URL
        url = f"{self.base_url}{path}"

        # Construct headers
        headers = {
            "Authorization": f"bearer {self.access_token}",
            "Content-Type": "application/json",
        }

        # Make request
        timeout = aiohttp.ClientTimeout(total=SOLAX_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if post:
                async with session.post(url, headers=headers, json=json_data) as response:
                    status = response.status
                    try:
                        data = await response.json()
                    except (aiohttp.ContentTypeError, json.JSONDecodeError) as e:
                        self.log(f"Warn: SolaX API: Failed to decode response from {path}: {e}")
                        self.error_count += 1
                        return None
            else:
                async with session.get(url, headers=headers, params=params) as response:
                    status = response.status
                    try:
                        data = await response.json()
                    except (aiohttp.ContentTypeError, json.JSONDecodeError) as e:
                        self.log(f"Warn: SolaX API: Failed to decode response from {path}: {e}")
                        self.error_count += 1
                        return None

            if status != 200:
                self.log(f"Warn: SolaX API: Request to {path} failed with status {status}")
                self.error_count += 1
                return None

            # Check for authentication errors in response
            code = data.get("code")
            if code in [10400, 10401, 10402]:  # Auth-related error codes
                error_desc = SOLAX_API_CODES.get(code, f"Unknown error code: {code}")
                self.log(f"Warn: SolaX API: Authentication error {code} ({error_desc}), marking token as expired")
                self.access_token = None
                self.token_expiry = None
                self.error_count += 1
                return None

            return data

    async def request_get(self, path, params=None, post=False, json_data=None):
        """
        Make GET/POST request with retry logic

        Args:
            path: API path
            params: URL parameters
            post: Use POST method
            json_data: JSON payload for POST

        Returns:
            Parsed JSON response or None
        """
        self.log("Solax: Request get path {} params {}".format(path, params))
        return await self.request_wrapper(lambda: self._request_get_impl(path, params, post, json_data))

    async def fetch_paginated_data(self, path, base_params, page_size=100):
        """
        Fetch paginated data from SolaX API

        Args:
            path: API endpoint path
            base_params: Base parameters dict (businessType, plantId, etc.)
            page_size: Records per page (default: 100)

        Returns:
            List of all records across pages, or None on failure
        """
        all_records = []
        page = 1

        while True:
            # Build request parameters with pagination
            params = base_params.copy()
            params["size"] = page_size
            params["pageNo"] = page

            # Make request
            response = await self.request_get(path, params=params)

            if response is None:
                self.log(f"Warn: SolaX API: Failed to fetch {path} page {page}")
                return None

            # Check response code
            code = response.get("code")
            is_error, code_desc = self.decode_api_code(code)
            if is_error:
                error_msg = response.get("message", "")
                self.log(f"Warn: SolaX API: Request to {path} failed with code {code}: {code_desc}")
                if error_msg:
                    self.log(f"  Details: {error_msg}")
                self.error_count += 1
                return None

            # Extract result
            result = response.get("result", {})
            records = result.get("records", [])
            pages = result.get("pages", 1)
            current = result.get("current", 1)
            total = result.get("total", 0)

            # Add records to list
            all_records.extend(records)

            self.log(f"SolaX API: Fetched {path} page {current}/{pages} ({len(records)} records, {total} total)")

            # Check if we have more pages
            if page >= pages:
                break

            page += 1

        return all_records

    async def fetch_single_result(self, path, params=None, post=False, json_data=None):
        """
        Fetch single (non-paginated) result from SolaX API

        Args:
            path: API endpoint path
            params: Request parameters dict (for GET)
            post: If True, use POST method
            json_data: JSON payload for POST requests

        Returns:
            Result dictionary or None on failure
        """
        # Make request
        if post:
            response = await self.request_get(path, post=True, json_data=json_data)
        else:
            response = await self.request_get(path, params=params)

        if response is None:
            self.log(f"Warn: SolaX API: Failed to fetch {path}")
            return None, None

        # Check response code
        code = response.get("code")
        is_error, code_desc = self.decode_api_code(code)
        if is_error:
            error_msg = response.get("message", "")
            self.log(f"Warn: SolaX API: Request to {path} failed with code {code}: {code_desc}")
            if error_msg:
                self.log(f"  Details: {error_msg}")
            self.error_count += 1
            return None, None

        # Extract and return result
        result = response.get("result", {})
        requestId = response.get("requestId", "")
        self.log(f"SolaX API: Fetched {path} successfully")
        return result, requestId

    async def query_plant_info(self):
        """
        Query plant information with pagination support

        Returns:
            List of plant records or None on failure
        """
        # Build base parameters
        params = {
            "businessType": BUSINESS_TYPE_RESIDENTIAL,
        }

        # Add plant ID filter if configured
        if self.plant_id:
            params["plantId"] = self.plant_id

        # Fetch paginated data
        result = await self.fetch_paginated_data("/openapi/v2/plant/page_plant_info", params)
        if result is not None:
            self.plant_info = result
        return result

    async def query_device_info(self, plant_id, device_type, device_sn=None, business_type=None):
        """
        Query device information with pagination support

        Args:
            plant_id: Optional plant ID to filter devices
            device_type: Optional device type (1=Inverter, 2=Battery, 3=Meter, 4=EV Charger)
            device_sn: Optional device serial number to filter
            business_type: Business type (1=Residential, 4=Commercial), defaults to initialized value

        Returns:
            List of device records or None on failure

        Example:

        INVERTER:
        {
            'deviceModel': 14,
            'armVersion': '1.51',
            'dspVersion': '1.55',
            'ratedPower': 10.0,
            'registerNo': 'SY1231321312',
            'deviceSn': 'H1231231932123',
            'plantId': '1618699116555534337',
            'onlineStatus': 1,
            'flag': 0
        }

        BATTERY:
        {
            'deviceModel': 1,
            'hardwareVersion': None,
            'registerNo': 'SY1231321312',
            'deviceSn': 'TP123456123123',
            'plantId': '1618699116555534337',
            'softwareVersion': '3.16',
            'ratedCapacity': 0.0,
            'onlineStatus': 1
        }

        """
        # Build base parameters
        params = {
            "businessType": business_type if business_type is not None else BUSINESS_TYPE_RESIDENTIAL,
        }

        # Add optional filters
        params["plantId"] = plant_id
        params["deviceType"] = device_type
        if device_sn:
            params["deviceSn"] = device_sn

        # Fetch paginated data
        result = await self.fetch_paginated_data("/openapi/v2/device/page_device_info", params)
        if result is not None:
            for device in result:
                deviceSn = device.get("deviceSn")
                if deviceSn:
                    device['deviceType'] = device_type
                    self.device_info[deviceSn] = device
                    # Store inverter SNs by plant
                    if plant_id not in self.plant_inverters:
                        self.plant_inverters[plant_id] = []
                    if plant_id not in self.plant_batteries:
                        self.plant_batteries[plant_id] = []
                    if device_type == 1 and deviceSn not in self.plant_inverters[plant_id]:
                        self.plant_inverters[plant_id].append(deviceSn)
                    elif device_type == 2 and deviceSn not in self.plant_batteries[plant_id]:
                        self.plant_batteries[plant_id].append(deviceSn)
                    self.log(f"Solax: Stored device info for SN: {deviceSn} info {device}")
        return result

    async def query_plant_realtime_data(self, plant_id, business_type=None):
        """
        Query real-time data from a specific plant

        Args:
            plant_id: Plant ID to query
            business_type: Business type (1=Residential, 4=Commercial), defaults to residential

        Returns:
            Dictionary with real-time data or None on failure

        Example:
            {
                'plantLocalTime': '2025-12-28 18:38:24',
                'plantId': '1618699116555534337',
                'dailyYield': 0.0,
                'totalYield': 31927.82,
                'dailyCharged': 0.0,
                'totalCharged': 7498.5,
                'dailyDischarged': 0.0,
                'totalDischarged': 6504.7,
                'dailyImported': 0.0,
                'totalImported': 17567.67,
                'dailyExported': 0.0,
                'totalExported': 15014.4,
                'dailyEarnings': 0.0,
                'totalEarnings': 2797.23
            }
        """
        # Build request parameters
        params = {
            "plantId": plant_id,
            "businessType": business_type if business_type is not None else BUSINESS_TYPE_RESIDENTIAL,
        }

        # Fetch single result
        result, requestId = await self.fetch_single_result("/openapi/v2/plant/realtime_data", params)
        if result is not None:
            self.log(f"SolaX API: Retrieved real-time data for plant ID {plant_id} {result}")
            self.realtime_data[plant_id] = result
        return result

    async def query_device_realtime_data_all(self, plant_id, business_type=None):
        results = []
        for device_sn in self.device_info:
            device_type = self.device_info[device_sn].get("deviceType")
            result = await self.query_device_realtime_data(device_sn, device_type, business_type)
            if result is not None:
                results.extend(result)
        return results

    async def query_plant_statistics_daily(self, plant_id, business_type=None):

        """
        Query daily statistical data for a specific plant for the current month

        Args:
            plant_id: Plant ID to query
            business_type: Business type (1=Residential, 4=Commercial), defaults to residential
        Returns:
            Dictionary with daily statistical data or None on failure
        """
        # Get current year and month
        now = datetime.now(timezone.utc)
        year_month = now.strftime("%Y-%m")

        # Call the general statistics query for monthly data
        result = await self.query_plant_statistics(
            plant_id=plant_id,
            date_type="2",  # Monthly data
            date=year_month,
            business_type=business_type
        )
        if result:
            self.log(f"SolaX API: Retrieved daily statistics for plant ID {plant_id} for {year_month}")
        return result


    async def query_plant_statistics(self, plant_id, date_type, date, business_type=None):
        """
        Query statistical data for a specific plant

        Args:
            plant_id: Plant ID to query
            date_type: Statistical dimension ("1"=Annual, "2"=Monthly)
            date: Statistical query date
                  - When date_type="1", format is year (e.g., "2025")
                  - When date_type="2", format is year-month (e.g., "2025-09")
            business_type: Business type (1=Residential, 4=Commercial), defaults to residential

        Returns:
            Dictionary with statistical data or None on failure

        Example response:
            {
                'plantId': '339663******618',
                'date': '2025',
                'currencyCode': 'SEK',
                'plantEnergyStatDataList': [
                    {
                        'date': '2025-05',
                        'pvGeneration': 190.60,
                        'inverterACOutputEnergy': 1183.10,
                        'exportEnergy': 9.80,
                        'importEnergy': 22.09,
                        'loadConsumption': 16861.63,
                        'batteryCharged': -58.40,
                        'batteryDischarged': 9.90,
                        'earnings': 11.11
                    },
                    ...
                ]
            }

        Notes:
            - Annual data (date_type="1"): Returns statistics for each month of the year
            - Monthly data (date_type="2"): Returns statistics for each day of that month
            - All energy values are in kWh
            - Date format in results:
              - Annual: "2025-09" (year-month)
              - Monthly: "2025-09-03" (year-month-day)
        """
        # Build POST body
        payload = {
            "plantId": plant_id,
            "dateType": date_type,
            "date": date,
            "businessType": business_type if business_type is not None else BUSINESS_TYPE_RESIDENTIAL,
        }

        # Fetch single result
        result, requestId = await self.fetch_single_result(
            "/openapi/v2/plant/energy/get_stat_data",
            post=True,
            json_data=payload
        )

        if result is not None:
            self.log(f"SolaX API: Retrieved statistics for plant ID {plant_id}, date {date}, type {date_type}")

        return result

    async def query_device_realtime_data(self, sn, device_type, business_type=None):
        # cSpell:disable
        """
        Query real-time data for specific devices

        Args:
            sn: Device serial number
            device_type: Device type (1=Inverter, 2=Battery, 3=Meter, 4=EV Charger)
            business_type: Business type (1=Residential, 4=Commercial), defaults to residential

        Returns:
            List of device realtime data dictionaries or None on failure

        Example Inverter response:
        [
            {
                'deviceStatus': 130,
                'gridPower': -4254.0,
                'todayImportEnergy': 16.8,
                'totalImportEnergy': 17679.3,
                'todayExportEnergy': 2.6,
                'totalExportEnergy': 15098.6,
                'gridPowerM2': 0.0,
                'todayImportEnergyM2': 0.0,
                'totalImportEnergyM2': 0.0,
                'todayExportEnergyM2': 0.0,
                'totalExportEnergyM2': 0.0,
                'dataTime': '2025-12-28T18:45:54.000+00:00',
                'plantLocalTime': '2025-12-28 19:45:54',
                'deviceSn': 'H1231231932123',
                'registerNo': 'SY1231321312',
                'acCurrent1': 1.0,
                'acVoltage1': 231.4,
                'acCurrent2': 1.1,
                'acCurrent3': 0.9,
                'acVoltage2': 229.8,
                'acVoltage3': 224.8,
                'acPower1': 15,
                'acPower2': 18,
                'acPower3': 9,
                'gridFrequency': None,
                'totalPowerFactor': 1.0,
                'inverterTemperature': 45.0,
                'dailyACOutput': 12.2,
                'totalACOutput': 32080.9,
                'dailyYield': 13.6,
                'totalYield': 33025.8,
                'mpptMap': {
                    'MPPT2Voltage': 0.0,
                    'MPPT1Current': 0.0,
                    'MPPT2Current': 0.0,
                    'MPPT1Voltage': 0.0,
                    'MPPT1Power': 0.0,
                    'MPPT2Power': 0.0,
                },
                'pvMap': {},
                'EPSL1Voltage': 0.0,
                'EPSL1Current': 0.0,
                'EPSL1ActivePower': 0,
                'EPSL2Voltage': 0.0,
                'EPSL2Current': 0.0,
                'EPSL2ActivePower': 0,
                'EPSL3Voltage': 0.0,
                'EPSL3Current': 0.0,
                'EPSL3ActivePower': 0,
                'EPSL1ApparentPower': 0,
                'EPSL2ApparentPower': 0,
                'EPSL3ApparentPower': 0,
                'l2l3Voltage': None,
                'l1l2Voltage': None,
                'l1l3Voltage': None,
                'totalReactivePower': 0,
                'totalActivePower': 0,
                'MPPTTotalInputPower': None
            }
        ]

        Example Battery response:
        [
            {
                'dataTime':
                '2025-12-28T18:45:54.000+00:00',
                'plantLocalTime': '2025-12-28 19:45:54',
                'deviceSn': 'TP123456123123',
                'registerNo': 'SY1231321312',
                'deviceStatus': 1,
                'batterySOC': 99,
                'batterySOH': 0,
                'chargeDischargePower': 0,
                'batteryVoltage': 426.9,
                'batteryCurrent': 0.0,
                'batteryTemperature': 22.0,
                'batteryCycleTimes': 652,
                'totalDeviceDischarge': 6537.8,
                'totalDeviceCharge': 7534.0,
                'batteryRemainings': 12.2
            }
        ]
        """
        # cSpell:enable
        # Build POST body
        params = {
            "snList": [sn],
            "deviceType": device_type,
            "businessType": business_type if business_type is not None else BUSINESS_TYPE_RESIDENTIAL,
        }

        # Fetch single result (returns array in this case)
        result, requestId = await self.fetch_single_result("/openapi/v2/device/realtime_data", params=params)
        if result is not None and len(result) > 0:
            # One result per device SN
            self.realtime_device_data[sn] = result[0]
            self.log(f"SolaX API: Retrieved real-time data for device SN {sn} {result[0]}")
            return result
        return None

    def is_a1_hybrid_g2(self, device_sn):
        """
        Check if device is A1-HYB-G2 inverter
        as these have different controls
        """
        device = self.device_info.get(device_sn, {})
        device_type = device.get("deviceType")
        device_model_code = device.get("deviceModel", 0)
        if device_type == 1:  # Inverter
            return device_model_code == 19  # A1-HYB-G2
        return False

    async def query_request_result(self, request_id):
        """
        Query the execution result of a control instruction

        Args:
            request_id: The requestId returned by a control instruction

        Returns:
            List of result dictionaries with 'sn' and 'status' fields, or None on failure

        Example response:
            [
                {'sn': 'X3******01', 'status': 'Command execution succeeded'},
                {'sn': 'X3******02', 'status': 'Device offline'}
            ]
        """
        # Build POST body
        payload = {
            "requestId": request_id,
        }

        # Make POST request
        response = await self.request_get("/openapi/apiRequestLog/listByCondition", post=True, json_data=payload)

        if response is None:
            self.log(f"Warn: SolaX API: Failed to query request result for requestId {request_id}")
            return None
        self.log("Solax: query_request_result response {}".format(response))

        code = response.get("code")
        error, error_description = self.decode_api_code(code)
        if error:
            error_msg = response.get("message", "")
            self.log(f"Warn: SolaX API: Request result query failed with code {code}: {error_msg}")
            self.error_count += 1
            return None

        # Extract and return result array
        result = response.get("result", [])
        self.log(f"SolaX API: Retrieved request result for requestId {request_id}: {len(result)} device(s)")
        status = SOLAX_COMMAND_STATUS_EXECUTION_SUCCESS
        for device_result in result:
            sn = device_result.get("sn")
            this_status = device_result.get("status")
            if this_status != status:
                self.log(f"  {sn}: {status}")
                status = this_status
                break

        return status

    async def send_command_and_wait(self, endpoint, payload, command_name, sn_list):
        """
        Send a control command and wait for execution result

        Args:
            endpoint: API endpoint path
            payload: Request payload dict
            command_name: Command name for logging (e.g., "selfuse", "backup")
            sn_list: List of device serial numbers for logging

        Returns:
            True if command executed successfully, False otherwise
        """
        # Fetch single result
        result, request_id = await self.fetch_single_result(
            endpoint,
            post=True,
            json_data=payload
        )
        self.log("Solax: send_command_and_wait result {}, request_id {}".format(result, request_id))

        if result is None:
            return False

        # Log results for each device
        status = SOLAX_COMMAND_STATUS_ISSUE_SUCCESS
        for sn in sn_list:
            this_status = result.get(sn, {}).get("status", SOLAX_COMMAND_STATUS_OFFLINE)
            if this_status != status:
                status = this_status
                break
        status_desc = SOLAX_COMMAND_STATUS.get(status, f"Unknown status {status}")
        self.log(f"SolaX API: Set {command_name} mode for {sn_list}: {status_desc} (requestId: {request_id})")

        # If command was issued successfully, wait for execution result
        if request_id and status == SOLAX_COMMAND_STATUS_ISSUE_SUCCESS:
            self.log(f"SolaX API: Waiting for execution result (requestId: {request_id})...")

            # Retry logic to wait for result
            for attempt in range(SOLAX_COMMAND_MAX_RETRIES):
                await asyncio.sleep(SOLAX_COMMAND_RETRY_DELAY + attempt)
                status = await self.query_request_result(request_id)

                if status is not None and (status > SOLAX_COMMAND_STATUS_ISSUE_SUCCESS):
                    if status == SOLAX_COMMAND_STATUS_EXECUTION_SUCCESS:
                        self.log(f"SolaX API: Command execution succeeded for requestId {request_id}")
                        return True
                    else:
                        self.log(f"SolaX API: Command execution failed with status {status} : {status_desc} for requestId {request_id}")
                        return False

                if attempt < SOLAX_COMMAND_MAX_RETRIES - 1:
                    self.log(f"SolaX API: Result not available yet, retrying ({attempt + 1}/{SOLAX_COMMAND_MAX_RETRIES})...")
                else:
                    self.log(f"Warn: SolaX API: Failed to retrieve execution result after {SOLAX_COMMAND_MAX_RETRIES} attempts")
                    return False

        if status == SOLAX_COMMAND_STATUS_EXECUTION_SUCCESS:
            self.log(f"SolaX API: Command execution succeeded for {sn}")
            return True

        # Command issuance failed or device offline
        self.log(f"Warn: SolaX API: Command issuance failed or device offline {sn} status {status} : {status_desc}")
        return False

    async def positive_or_negative_mode(self, sn, battery_power, time_of_duration, next_motion=161,
                                        business_type=None):
        """
        Set inverter working mode to Positive or Negative mode
        This mode directly controls the battery charging/discharging power

        AKA: Charge or Export Mode

        Args:
            sn: Device serial number
            battery_power: Battery charge/discharge power target (positive for discharge, negative for charge) in Watts
            time_of_duration: Mode duration time in seconds
            next_motion: Action after execution mode ends (160=Exit Remote Control, 161=Back to Self-Consume Mode)
            business_type: Business type (1=Residential, 4=Commercial), defaults to residential

        Returns:
            True if command executed successfully, False otherwise

        Notes:
            - Positive battery_power = discharge
            - Negative battery_power = charge
            - PV power is maximized
            - System can feed/take power to/from grid
        """
        # Build POST body
        payload = {
            "snList": [sn],
            "batteryPower": battery_power,
            "timeOfDuration": time_of_duration,
            "nextMotion": next_motion,
            "businessType": business_type if business_type is not None else BUSINESS_TYPE_RESIDENTIAL,
        }

        endpoint = "/openapi/v2/device/inverter_vpp_mode/push_power/positive_or_negative_mode"

        # Send command and wait for result
        return await self.send_command_and_wait(endpoint, payload, "positive/negative", sn)

    async def self_consume_mode(self, sn_list, time_of_duration, next_motion=161,
                                business_type=None):
        """
        Set inverter working mode to Self-Consume Charge/Discharge Mode
        Default remote control mode with PV-only charging

        AKA: ECO Mode

        Args:
            sn_list: List of device serial numbers
            time_of_duration: Mode duration time in seconds
            next_motion: Action after execution mode ends (160=Exit Remote Control, 161=Back to Self-Consume Mode)
            business_type: Business type (1=Residential, 4=Commercial), defaults to residential

        Returns:
            True if command executed successfully, False otherwise

        Notes:
            - Battery charged from PV only (no grid charging)
            - Battery discharge depends on load and PV availability
            - If PV cannot cover load, battery discharges
            - If battery full, excess PV feeds to grid
            - Priority: load > battery > grid (like Self-Use mode)
        """
        # Build POST body
        payload = {
            "snList": sn_list,
            "timeOfDuration": time_of_duration,
            "nextMotion": next_motion,
            "businessType": business_type if business_type is not None else BUSINESS_TYPE_RESIDENTIAL,
        }

        endpoint = "/openapi/v2/device/inverter_vpp_mode/self_consume/charge_or_discharge_mode"

        # Send command and wait for result
        return await self.send_command_and_wait(endpoint, payload, "self-consume", sn_list)



    async def self_consume_charge_only_mode(self, sn_list, time_of_duration, next_motion=161,
                                            business_type=None):
        """
        Set inverter working mode to Self-Consume Charge Only Mode
        Battery charges from PV only, discharge not allowed

        AKA: Freeze Charge

        Args:
            sn_list: List of device serial numbers
            time_of_duration: Mode duration time in seconds
            next_motion: Action after execution mode ends (160=Exit Remote Control, 161=Back to Self-Consume Mode)
            business_type: Business type (1=Residential, 4=Commercial), defaults to residential

        Returns:
            True if command executed successfully, False otherwise

        Notes:
            - Battery charged from PV only (no grid charging)
            - Battery discharge NOT allowed
            - Import from grid if necessary to cover load
            - Export to grid if battery is full
            - Priority: load > battery > grid
            - Useful for building battery reserve during sunny periods
        """
        # Build POST body
        payload = {
            "snList": sn_list,
            "timeOfDuration": time_of_duration,
            "nextMotion": next_motion,
            "businessType": business_type if business_type is not None else BUSINESS_TYPE_RESIDENTIAL,
        }

        endpoint = "/openapi/v2/device/inverter_vpp_mode/self_consume/charge_only_mode"

        # Send command and wait for result
        return await self.send_command_and_wait(endpoint, payload, "self-consume-charge-only", sn_list)

    async def exit_vpp_mode(self, sn_list, business_type=None):
        """
        Exit remote control mode

        Batch set inverter to exit remote control (VPP mode).

        Args:
            sn_list: List of device serial numbers (minimum 1, maximum 10 devices)
            business_type: Business type (1=Residential, 4=Commercial), defaults to residential

        Returns:
            True if command executed successfully, False otherwise

        Notes:
            - Exits remote control mode for all specified inverters
            - Inverters return to their default operation mode
            - Can control 1-10 devices in a single request
        """
        # Build POST body
        payload = {
            "snList": sn_list,
            "businessType": business_type if business_type is not None else BUSINESS_TYPE_RESIDENTIAL,
        }

        endpoint = "/openapi/v2/device/inverter_vpp_mode/exit_vpp_mode"

        # Send command and wait for result
        return await self.send_command_and_wait(endpoint, payload, "exit-vpp-mode", sn_list)

    async def set_default_work_mode(self, sn_list, business_type=None, mode="selfuse"):
        success = await self.set_work_mode(mode, sn_list, 10, 100, 0, "00:00", "00:00", "00:00", "23:59", business_type=business_type)
        if success:
            self.log(f"SolaX API: Set default work mode to {mode} for device {sn_list}")
        else:
            self.log(f"Warn: SolaX API: Failed to set default work mode to {mode} for device {sn_list}")
        return success

    async def set_work_mode(self, mode, sn_list, min_soc, charge_upper_soc, charge_from_grid_enable,
                                charge_start_time, charge_end_time,
                                discharge_start_time, discharge_end_time,
                                business_type=None):
        """
        Set inverter working mode to Self Use Mode (time period 1 only)

        Args:
            mode: Must be 'selfuse', 'backup' or 'feedin'
            sn: Device serial number
            min_soc: Minimum SOC (0-100)
            charge_upper_soc: Charging limit SOC (0-100)
            charge_from_grid_enable: Whether to allow import from grid (0=Not allowed, 1=Allowed)
            charge_start_time: Start time of charge period 1 (format: "HH:MM")
            charge_end_time: End time of charge period 1 (format: "HH:MM")
            discharge_start_time: Start time of discharge period 1 (format: "HH:MM")
            discharge_end_time: End time of discharge period 1 (format: "HH:MM")
            business_type: Business type (1=Residential, 4=Commercial), defaults to residential
            wait_for_result: If True, wait for execution result (default: False)

        Returns:
            Dictionary with command results and optionally execution results, or None on failure

        Example response (wait_for_result=False):
            {
                'X3******01': {'status': 3},
                'requestId': '66******66'
            }

        Example response (wait_for_result=True):
            {
                'X3******01': {'status': 3},
                'requestId': '66******66',
                'execution_result': [
                    {'sn': 'X3******01', 'status': 'Command execution succeeded'}
                ]
            }

        Status codes (from SOLAX_COMMAND_STATUS):
            1: Device Offline
            2: Command issuance failed
            3: Command issuance succeeded
            4: Device execution succeeded
            5: Device execution failed
            6: Execution timed out
        """

        # Build POST body
        payload = {
            "snList": sn_list,
            "businessType": business_type if business_type is not None else BUSINESS_TYPE_RESIDENTIAL,
            "minSoc": min_soc,
            "chargeUpperSoc": charge_upper_soc,
            "chargeFromGridEnable": charge_from_grid_enable,
            "chargeStartTimePeriod1": charge_start_time,
            "chargeEndTimePeriod1": charge_end_time,
            "dischargeStartTimePeriod1": discharge_start_time,
            "dischargeEndTimePeriod1": discharge_end_time,
            "enableTimePeriod2": 0,  # Only use time period 1
            "chargeStartTimePeriod2": "00:00",
            "chargeEndTimePeriod2": "00:00",
            "dischargeStartTimePeriod2": "00:00",
            "dischargeEndTimePeriod2": "00:00",
        }
        if mode == "selfuse":
            endpoint = "/openapi/v2/device/inverter_work_mode/batch_set_spontaneity_self_use"
        elif mode == "backup":
            endpoint = "/openapi/v2/device/inverter_work_mode/batch_set_peace_mode"
        elif mode == "feedin":
            endpoint = "/openapi/v2/device/inverter_work_mode/batch_set_on_grid_first"
        else:
            self.log(f"Warn: SolaX API: Unknown mode '{mode}'")
            return False

        # Send command and wait for result
        return await self.send_command_and_wait(endpoint, payload, mode, sn_list)

    async def soc_target_control_mode(self, sn_list, target_soc, charge_discharge_power,
                                      business_type=None):
        """
        Set inverter working mode to SOC Target Control Mode
        Controls AC port power with battery SOC as target

        Args:
            sn_list: List of device serial numbers
            target_soc: Target SOC percentage (0-100)
            charge_discharge_power: AC active power target in Watts
                                   (positive for absorbing/charging, negative for outputting/discharging)
            business_type: Business type (1=Residential, 4=Commercial), defaults to residential

        Returns:
            True if command executed successfully, False otherwise

        Notes:
            - PV runs at highest possible power
            - System can feed/take power to/from grid
            - Positive charge_discharge_power = absorbing power (charging)
            - Negative charge_discharge_power = outputting power (discharging)
            - Mode exits automatically if target contradicts current state
              (e.g., current SOC 50%, target 40%, but command requires charging)
        """
        # Build POST body
        payload = {
            "snList": sn_list,
            "targetSoc": target_soc,
            "chargeDischargPower": charge_discharge_power, # cSpell:disable-line
            "businessType": business_type if business_type is not None else BUSINESS_TYPE_RESIDENTIAL,
        }

        endpoint = "/openapi/v2/device/inverter_vpp_mode/soc_target_control_mode"

        # Send command and wait for result
        return await self.send_command_and_wait(endpoint, payload, "soc-target", sn_list)

    async def publish_device_info(self):
        # Publish per-device sensors
        for device_sn, device in self.device_info.items():
            device_type = device.get("deviceType")
            plant_id = device.get("plantId", "unknown").lower().replace(" ", "_")
            device_model_code = device.get("deviceModel", 0)
            online_status = device.get("onlineStatus", 0)
            ratedPower = device.get("ratedPower", 0)

            if device_type == 1:  # Inverter
                device_model = SOLAX_DEVICE_MODEL_RESIDENTIAL.get(1, {}).get(device_model_code, "Inverter")
            elif device_type == 2:  # Battery
                device_model = SOLAX_DEVICE_MODEL_RESIDENTIAL.get(2, {}).get(device_model_code, "Battery")
            elif device_type == 3:  # Meter
                device_model = SOLAX_DEVICE_MODEL_RESIDENTIAL.get(3, {}).get(device_model_code, "Meter")
            elif device_type == 4:  # EV Charger
                device_model = SOLAX_DEVICE_MODEL_RESIDENTIAL.get(4, {}).get(device_model_code, "EV Charger")
            else:
                device_model = "Unknown Device"

            friendly_name = f"SolaX {device_model} {device_sn}"

            # Online status sensor
            self.dashboard_item(
                f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_online_status",
                state=online_status,
                attributes={
                    "friendly_name": f"{friendly_name} Online Status",
                    "device_class": "connectivity",
                    "plant_id": device.get("plantId"),
                    "device_sn": device_sn,
                    "device_type": device_type,
                    "device_model": device_model,
                    "rated_power": ratedPower * 1000,
                },
                app="solax",
            )

    async def publish_device_realtime_data(self):
        """
        Publish data on INVERTER and BATTERY device extracted with query_device_realtime_data_all()
        """
        # Publish per-device realtime data
        for device_sn, realtime in self.realtime_device_data.items():
            device = self.device_info.get(device_sn, {})
            device_type = device.get("deviceType")
            plant_id = device.get("plantId", "unknown").lower().replace(" ", "_")
            device_model_code = device.get("deviceModel", 0)

            if device_type == 1:  # Inverter
                device_model = SOLAX_DEVICE_MODEL_RESIDENTIAL.get(1, {}).get(device_model_code, "Unknown Inverter")
            elif device_type == 2:  # Battery
                device_model = SOLAX_DEVICE_MODEL_RESIDENTIAL.get(2, {}).get(device_model_code, "Unknown Battery")
            else:
                device_model = "Unknown Device"

            friendly_name = f"SolaX {device_model} {device_sn}"

            load_power = 0

            if device_type == 1:  # Inverter
                ac_power1 = realtime.get("acPower1", 0)
                ac_power2 = realtime.get("acPower2", 0)
                ac_power3 = realtime.get("acPower3", 0)
                ac_power = (ac_power1 if ac_power1 else 0) + (ac_power2 if ac_power2 else 0) + (ac_power3 if ac_power3 else 0)
                gridPower = realtime.get("gridPower", 0)
                pvMap = realtime.get("pvMap", {})
                mpptMap = realtime.get("mpptMap", {}) # cSpell:disable-line
                totalActivePower = realtime.get("totalActivePower", 0)
                totalReactivePower = realtime.get("totalReactivePower", 0)
                totalYield = realtime.get("totalYield", 0)
                deviceStatus = realtime.get("deviceStatus", 0)
                deviceStatusText = SOLAX_INVERTER_STATUS.get(deviceStatus, "Unknown Status")

                # Calculate total PV power from pvMap
                pvPower = 0
                if pvMap:
                    for key in pvMap:
                        if "Power" in key:
                            pvPower += pvMap.get(key, 0)
                elif mpptMap: # cSpell:disable-line
                    for key in mpptMap: # cSpell:disable-line
                        if "Power" in key:
                            pvPower += mpptMap.get(key, 0) # cSpell:disable-line

                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_device_status",
                    state=deviceStatusText,
                    attributes={
                        "friendly_name": f"{friendly_name} Device Status",
                        "device_class": "status",
                        "state_class": "measurement",
                        "status_value": deviceStatus,
                    },
                    app="solax",
                )
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_ac_power",
                    state=ac_power,
                    attributes={
                        "friendly_name": f"{friendly_name} AC Power",
                        "unit_of_measurement": "W",
                        "device_class": "power",
                        "state_class": "measurement",
                    },
                    app="solax",
                )
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_grid_power",
                    state=gridPower,
                    attributes={
                        "friendly_name": f"{friendly_name} Grid Power",
                        "unit_of_measurement": "W",
                        "device_class": "power",
                        "state_class": "measurement",
                    },
                    app="solax",
                )

                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_pv_power",
                    state=pvPower,
                    attributes={
                        "friendly_name": f"{friendly_name} PV Power",
                        "unit_of_measurement": "W",
                        "device_class": "power",
                        "state_class": "measurement",
                    },
                    app="solax",
                )

                # This is inverter power, positive indicates export to grid, negative indicates import from grid.
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_total_active_power",
                    state=totalActivePower,
                    attributes={
                        "friendly_name": f"{friendly_name} Total Active Power",
                        "unit_of_measurement": "W",
                        "device_class": "power",
                        "state_class": "measurement",
                    },
                    app="solax",
                )
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_total_reactive_power",
                    state=totalReactivePower,
                    attributes={
                        "friendly_name": f"{friendly_name} Total Reactive Power",
                        "unit_of_measurement": "W",
                        "device_class": "power",
                        "state_class": "measurement",
                    },
                    app="solax",
                )
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_total_yield",
                    state=totalYield,
                    attributes={
                        "friendly_name": f"{friendly_name} Total Yield",
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "measurement",
                    },
                    app="solax",
                )
            elif device_type == 2:  # Battery
                battery_soc = realtime.get("batterySOC", 0)
                battery_voltage = realtime.get("batteryVoltage", 0)
                charge_discharge_power = realtime.get("chargeDischargePower", 0)
                battery_current = realtime.get("batteryCurrent", 0)
                battery_temperature = realtime.get("batteryTemperature", 0)
                deviceStatus = realtime.get("deviceStatus", 0)
                deviceStatusText = SOLAX_BATTERY_STATUS_RESIDENTIAL.get(deviceStatus, "Unknown Status")

                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_device_status",
                    state=deviceStatusText,
                    attributes={
                        "friendly_name": f"{friendly_name} Device Status",
                        "device_class": "status",
                        "state_class": "measurement",
                        "status_value": deviceStatus,
                    },
                    app="solax",
                )
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_battery_soc",
                    state=battery_soc,
                    attributes={
                        "friendly_name": f"{friendly_name} Battery SOC",
                        "unit_of_measurement": "%",
                        "device_class": "battery",
                        "state_class": "measurement",
                    },
                    app="solax",
                )
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_battery_voltage",
                    state=battery_voltage,
                    attributes={
                        "friendly_name": f"{friendly_name} Battery Voltage",
                        "unit_of_measurement": "V",
                        "device_class": "voltage",
                        "state_class": "measurement",
                    },
                    app="solax",
                )
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_charge_discharge_power",
                    state=charge_discharge_power,
                    attributes={
                        "friendly_name": f"{friendly_name} Charge/Discharge Power",
                        "unit_of_measurement": "W",
                        "device_class": "power",
                        "state_class": "measurement",
                    },
                    app="solax",
                )
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_battery_current",
                    state=battery_current,
                    attributes={
                        "friendly_name": f"{friendly_name} Battery Current",
                        "unit_of_measurement": "A",
                        "device_class": "current",
                        "state_class": "measurement",
                    },
                    app="solax",
                )
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_{device_sn}_battery_temperature",
                    state=battery_temperature,
                    attributes={
                        "friendly_name": f"{friendly_name} Battery Temperature",
                        "unit_of_measurement": "°C",
                        "device_class": "temperature",
                        "state_class": "measurement",
                    },
                    app="solax",
                )

    async def publish_plant_info(self):
        # Publish per-plant sensors
        for plant in self.plant_info:
            plant_id = plant.get("plantId", "unknown").lower().replace(" ", "_")
            plant_name = plant.get("plantName", "Unknown")

            inverter_max_power = self.get_max_power_inverter(plant_id)
            battery_max_power = self.get_max_power_battery(plant_id)
            battery_soc_max = self.get_max_soc_battery(plant_id)
            battery_soc, battery_size_max_approx = self.get_current_soc_battery_kwh(plant_id)
            battery_temp = self.get_battery_temperature(plant_id)
            charge_discharge_power = self.get_charge_discharge_power_battery(plant_id)

            # Battery SOC
            self.dashboard_item(
                f"sensor.{self.prefix}_solax_{plant_id}_battery_soc",
                state=battery_soc,
                attributes={
                    "friendly_name": f"SolaX {plant_name} Battery SOC",
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "measurement",
                    "soc_max": battery_soc_max,
                },
                app="solax",
            )
            # Battery Charge/Discharge Power
            self.dashboard_item(
                f"sensor.{self.prefix}_solax_{plant_id}_battery_charge_discharge_power",
                state=charge_discharge_power,
                attributes={
                    "friendly_name": f"SolaX {plant_name} Battery Charge/Discharge Power",
                    "unit_of_measurement": "W",
                    "device_class": "power",
                    "state_class": "measurement",
                },
                app="solax",
            )
            # Battery SOC max sensor
            self.dashboard_item(
                f"sensor.{self.prefix}_solax_{plant_id}_battery_capacity",
                state=battery_soc_max,
                attributes={
                    "friendly_name": f"SolaX {plant_name} Battery Capacity",
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "measurement",
                },
                app="solax",
            )

            # Battery temperature sensor
            self.dashboard_item(
                f"sensor.{self.prefix}_solax_{plant_id}_battery_temperature",
                state=battery_temp,
                attributes={
                    "friendly_name": f"SolaX {plant_name} Battery Temperature",
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "state_class": "measurement",
                },
                app="solax",
            )

            # Battery max power sensor
            self.dashboard_item(
                f"sensor.{self.prefix}_solax_{plant_id}_battery_max_power",
                state=battery_max_power,
                attributes={
                    "friendly_name": f"SolaX {plant_name} Battery Max Power",
                    "unit_of_measurement": "W",
                    "device_class": "power",
                    "state_class": "measurement",
                },
                app="solax",
            )
            # Inverter max power sensor
            self.dashboard_item(
                f"sensor.{self.prefix}_solax_{plant_id}_inverter_max_power",
                state=inverter_max_power,
                attributes={
                    "friendly_name": f"SolaX {plant_name} Inverter Max Power",
                    "unit_of_measurement": "W",
                    "device_class": "power",
                    "state_class": "measurement",
                },
                app="solax",
            )

            # PV capacity sensor
            pv_capacity = plant.get("pvCapacity", 0.0)
            self.dashboard_item(
                f"sensor.{self.prefix}_solax_{plant_id}_pv_capacity",
                state=pv_capacity,
                attributes={
                    "friendly_name": f"SolaX {plant_name} PV Capacity",
                    "unit_of_measurement": "kWp",
                    "device_class": "power",
                    "state_class": "measurement",
                },
                app="solax",
            )

            # Publish realtime data if available
            realtime_plant_id = plant.get("plantId")
            if realtime_plant_id and realtime_plant_id in self.realtime_data:
                realtime = self.realtime_data[realtime_plant_id]

                # Total Yield sensor
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_total_yield",
                    state=realtime.get("totalYield", 0.0),
                    attributes={
                        "friendly_name": f"SolaX {plant_name} Total Yield",
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "measurement",
                    },
                    app="solax",
                )

                # Total Charged sensor
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_total_charged",
                    state=realtime.get("totalCharged", 0.0),
                    attributes={
                        "friendly_name": f"SolaX {plant_name} Total Charged",
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "measurement",
                    },
                    app="solax",
                )

                # Total Discharged sensor
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_total_discharged",
                    state=realtime.get("totalDischarged", 0.0),
                    attributes={
                        "friendly_name": f"SolaX {plant_name} Total Discharged",
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "measurement",
                    },
                    app="solax",
                )

                # Total Imported sensor
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_total_imported",
                    state=realtime.get("totalImported", 0.0),
                    attributes={
                        "friendly_name": f"SolaX {plant_name} Total Imported",
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "measurement",
                    },
                    app="solax",
                )

                # Total Exported sensor
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_total_exported",
                    state=realtime.get("totalExported", 0.0),
                    attributes={
                        "friendly_name": f"SolaX {plant_name} Total Exported",
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "measurement",
                    },
                    app="solax",
                )
                # Work out total load
                # This is will total imported + total discharged - total exported - total charged + total yield
                total_load = (
                    realtime.get("totalImported", 0.0) +
                    realtime.get("totalDischarged", 0.0) -
                    realtime.get("totalExported", 0.0) -
                    realtime.get("totalCharged", 0.0) +
                    realtime.get("totalYield", 0.0)
                )
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_total_load",
                    state=total_load,
                    attributes={
                        "friendly_name": f"SolaX {plant_name} Total Load",
                        "unit_of_measurement": "kWh",
                        "device_class": "energy",
                        "state_class": "measurement",
                    },
                    app="solax",
                )

                # Total Earnings sensor
                self.dashboard_item(
                    f"sensor.{self.prefix}_solax_{plant_id}_total_earnings",
                    state=realtime.get("totalEarnings", 0.0),
                    attributes={
                        "friendly_name": f"SolaX {plant_name} Total Earnings",
                        "unit_of_measurement": "currency",
                        "state_class": "measurement",
                    },
                    app="solax",
                )

    async def run(self, seconds, first):
        """
        Main run loop called every 5 seconds

        Args:
            seconds: Seconds counter
            first: True on first run

        Returns:
            True on success, False on failure
        """
        if first:
            # Fetch plant information on startup
            self.log("SolaX API: Fetching plant information...")
            await self.query_plant_info()

            if self.plant_info is None:
                self.log("Warn: SolaX API: Failed to fetch plant information")
                return False

            self.plant_list = [plant.get('plantId') for plant in self.plant_info]
            if self.plant_sn_filter:
                self.plant_list = [pid for pid in self.plant_list if pid in self.plant_sn_filter]
            self.log(f"SolaX API: Found {len(self.plant_list)} plants IDs: {self.plant_list}")

        # Check readonly mode
        is_readonly = self.get_state_wrapper(f'switch.{self.prefix}_set_read_only', default='off') == 'on'

        if first or seconds % (30*60) == 0:
            # Periodic plant info refresh every 30 minutes
            for plantID in self.plant_list:
                self.log(f"SolaX API: Fetching device information for plant ID {plantID}...")
                await self.query_device_info(plantID, device_type=SOLAX_DEVICE_TYPE_INVERTER)  # Inverter
                await self.query_device_info(plantID, device_type=SOLAX_DEVICE_TYPE_BATTERY)  # Battery
                #await self.query_device_info(plantID, device_type=SOLAX_DEVICE_TYPE_METER)  # Meter
                #await self.query_plant_statistics_daily(plantID)

        if first or seconds % 60 == 0:
            for plantID in self.plant_list:
                await self.query_plant_realtime_data(plantID)
                await self.query_device_realtime_data_all(plantID)

        # Fetch controls first time only
        if first:
            for plantID in self.plant_list:
                await self.fetch_controls(plant_id=plantID)

        # Publish
        if first or seconds % 60 == 0:
            await self.publish_plant_info()
            await self.publish_device_info()
            await self.publish_device_realtime_data()
            await self.publish_controls()

        # Automatic configuration
        if first and self.automatic:
            await self.automatic_config()

        # Apply controls
        if not is_readonly and self.enable_controls:
            # Control
            if first or seconds % 60 == 0:
                for plantID in self.plant_list:
                    await self.apply_controls(plantID)
        else:
            self.log("SolaX API: Read-only mode enabled, skipping control application")

        # Update success timestamp
        self.update_success_timestamp()

        return True


class MockBase: # pragma: no cover
    """Mock base class for standalone testing"""

    def __init__(self):
        self.prefix = "predbat"
        self.local_tz = timezone.utc
        self.args = {}
        self.entities = {}

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=None):
        if raw:
            return self.entities.get(entity_id, {})
        else:
            return self.entities.get(entity_id, {}).get('state', default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None):
        self.entities[entity_id] = {
            'state': state,
            'attributes': attributes or {}
        }

    def log(self, message):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        print(f"ENTITY: {entity_id} = {state}")
        if attributes:
            if 'options' in attributes:
                attributes['options'] = '...'
            print(f"  Attributes: {json.dumps(attributes, indent=2)}")
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, key, default=None):
        return default

    def set_arg(self, key, value):
        state = None
        if isinstance(value, str) and '.' in value:
            state = self.get_state_wrapper(value, default=None)
        elif isinstance(value, list):
            state = "n/a []"
            for v in value:
                if isinstance(v, str) and '.' in v:
                    state = self.get_state_wrapper(v, default=None)
                    break
        else:
            state = "n/a"
        print(f"Set arg {key} = {value} (state={state})")


async def test_solax_api(client_id, client_secret, region, plant_id, test_mode=None): # pragma: no cover
    """
    Test function for standalone execution

    Args:
        client_id: SolaX client ID
        client_secret: SolaX client secret
        region: API region
        plant_id: Optional plant ID filter
        test_mode: Optional control mode to test ('eco', 'charge', 'freeze_charge', 'export', 'freeze_export')
    """
    print(f"\n{'=' * 60}")
    print(f"Testing SolaX API")
    print(f"Region: {region}")
    print(f"Client ID: {client_id[:10]}...")
    if plant_id:
        print(f"Plant ID filter: {plant_id}")
    if test_mode:
        print(f"Test mode: {test_mode}")
    print(f"{'=' * 60}\n")

    # Create mock base
    mock_base = MockBase()

    # Create SolaX API instance
    enable_controls = test_mode is not None
    solax = SolaxAPI(
        mock_base,
        client_id=client_id,
        client_secret=client_secret,
        region=region,
        plant_id=plant_id,
        automatic=True,
        enable_controls=enable_controls,
    )
    result = await solax.run(first=True, seconds=0)
    if not result:
        print("✗ Initialization failed")
        return
    else:
        print("✓ Initialization successful")

    # If test_mode is specified, apply controls
    if test_mode and solax.plant_list:
        test_plant_id = solax.plant_list[0]
        print(f"\n{'=' * 60}")
        print(f"Testing control mode: {test_mode}")
        print(f"Plant ID: {test_plant_id}")
        print(f"{'=' * 60}\n")

        # Mock the controls structure based on the test mode
        now = datetime.now(solax.local_tz)

        if test_mode == "eco":
            # Set up controls with no active windows
            solax.controls[test_plant_id] = {
                "reserve": 10,
                "charge": {"start_time": "23:00:00", "end_time": "23:30:00", "enable": False, "target_soc": 100, "rate": 5000},
                "export": {"start_time": "23:30:00", "end_time": "23:59:00", "enable": False, "target_soc": 10, "rate": 5000},
            }
            print("✓ Configured for ECO mode (no active windows)")

        elif test_mode == "charge":
            # Set up controls with active charge window
            charge_start = now - timedelta(minutes=30)
            charge_end = now + timedelta(hours=2)
            solax.controls[test_plant_id] = {
                "reserve": 10,
                "charge": {
                    "start_time": charge_start.strftime("%H:%M:%S"),
                    "end_time": charge_end.strftime("%H:%M:%S"),
                    "enable": True,
                    "target_soc": 95,
                    "rate": 5000
                },
                "export": {"start_time": "23:30:00", "end_time": "23:59:00", "enable": False, "target_soc": 10, "rate": 5000},
            }
            print(f"✓ Configured for CHARGE mode ({charge_start.strftime('%H:%M')} - {charge_end.strftime('%H:%M')}, target: 95%)")

        elif test_mode == "freeze_charge":
            # Set up controls with charge window but current SOC = target SOC
            charge_start = now - timedelta(minutes=30)
            charge_end = now + timedelta(hours=2)
            current_soc_kwh, max_soc_kwh = solax.get_current_soc_battery_kwh(test_plant_id)
            current_soc = int((current_soc_kwh / max_soc_kwh) * 100) if max_soc_kwh > 0 else 50
            solax.controls[test_plant_id] = {
                "reserve": 10,
                "charge": {
                    "start_time": charge_start.strftime("%H:%M:%S"),
                    "end_time": charge_end.strftime("%H:%M:%S"),
                    "enable": True,
                    "target_soc": current_soc,  # Same as current = freeze
                    "rate": 5000
                },
                "export": {"start_time": "23:30:00", "end_time": "23:59:00", "enable": False, "target_soc": 10, "rate": 5000},
            }
            print(f"✓ Configured for FREEZE CHARGE mode ({charge_start.strftime('%H:%M')} - {charge_end.strftime('%H:%M')}, current SOC: {current_soc}%)")

        elif test_mode == "export":
            # Set up controls with active export window
            export_start = now - timedelta(minutes=30)
            export_end = now + timedelta(hours=2)
            solax.controls[test_plant_id] = {
                "reserve": 10,
                "charge": {"start_time": "23:00:00", "end_time": "23:30:00", "enable": False, "target_soc": 100, "rate": 5000},
                "export": {
                    "start_time": export_start.strftime("%H:%M:%S"),
                    "end_time": export_end.strftime("%H:%M:%S"),
                    "enable": True,
                    "target_soc": 15,
                    "rate": 4500
                },
            }
            print(f"✓ Configured for EXPORT mode ({export_start.strftime('%H:%M')} - {export_end.strftime('%H:%M')}, target: 15%)")

        elif test_mode == "freeze_export":
            # Set up controls with export window but target SOC >= current SOC
            export_start = now - timedelta(minutes=30)
            export_end = now + timedelta(hours=2)
            current_soc_kwh, max_soc_kwh = solax.get_current_soc_battery_kwh(test_plant_id)
            current_soc = int((current_soc_kwh / max_soc_kwh) * 100) if max_soc_kwh > 0 else 50
            target_soc = min(100, current_soc + 10)  # Higher than current = freeze
            solax.controls[test_plant_id] = {
                "reserve": 10,
                "charge": {"start_time": "23:00:00", "end_time": "23:30:00", "enable": False, "target_soc": 100, "rate": 5000},
                "export": {
                    "start_time": export_start.strftime("%H:%M:%S"),
                    "end_time": export_end.strftime("%H:%M:%S"),
                    "enable": True,
                    "target_soc": target_soc,  # Higher than current = freeze
                    "rate": 4500
                },
            }
            print(f"✓ Configured for FREEZE EXPORT mode ({export_start.strftime('%H:%M')} - {export_end.strftime('%H:%M')}, current: {current_soc}%, target: {target_soc}%)")

        else:
            print(f"✗ Unknown test mode: {test_mode}")
            return 1

        # Apply the controls
        print("\nApplying controls...")
        result = await solax.apply_controls(test_plant_id)
        if result:
            print("✓ Controls applied successfully")
        else:
            print("✗ Controls application failed")
            return 1

    return 0


def main(): # pragma: no cover
    """Main entry point for standalone testing"""
    parser = argparse.ArgumentParser(
        description="Test SolaX Cloud API and control modes",
        epilog="Example: python solax.py --client-id YOUR_ID --client-secret YOUR_SECRET --test-mode charge",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--client-id", required=True, help="SolaX Cloud client ID")
    parser.add_argument("--client-secret", required=True, help="SolaX Cloud client secret")
    parser.add_argument("--region", default="eu", choices=["eu", "us", "cn"], help="API region (default: eu)")
    parser.add_argument("--plant-id", help="Optional plant ID to filter")
    parser.add_argument("--test-mode", choices=["eco", "charge", "freeze_charge", "export", "freeze_export"],
                       help="Test control mode: eco (no windows), charge (active charge), freeze_charge (at target), export (active export), freeze_export (at/above target)")

    args = parser.parse_args()

    asyncio.run(test_solax_api(args.client_id, args.client_secret, args.region, args.plant_id, args.test_mode))


if __name__ == "__main__": # pragma: no cover
    main()
