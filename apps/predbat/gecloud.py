# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

import requests
from datetime import timedelta, datetime, timezone
from utils import str2time, dp1
import asyncio
import random
import time

"""
GE Cloud data download
"""

GE_API_URL = "https://api.givenergy.cloud/v1/"
GE_API_INVERTER_STATUS = "inverter/{inverter_serial_number}/system-data/latest"
GE_API_INVERTER_METER = "inverter/{inverter_serial_number}/meter-data/latest"
GE_API_INVERTER_SETTINGS = "inverter/{inverter_serial_number}/settings"
GE_API_INVERTER_READ_SETTING = "inverter/{inverter_serial_number}/settings/{setting_id}/read"
GE_API_INVERTER_WRITE_SETTING = "inverter/{inverter_serial_number}/settings/{setting_id}/write"
GE_API_DEVICES = "communication-device"
GE_API_DEVICE_INFO = "communication-device"
GE_API_SMART_DEVICES = "smart-device"
GE_API_SMART_DEVICE = "smart-device/{uuid}"
GE_API_SMART_DEVICE_DATA = "smart-device/{uuid}/data"
GE_API_EVC_DEVICES = "ev-charger"
GE_API_EVC_DEVICE = "ev-charger/{uuid}"
GE_API_EVC_DEVICE_DATA = "ev-charger/{uuid}/meter-data?start_time={start_time}&end_time={end_time}&meter_ids[]={meter_ids}&page=1"
GE_API_EVC_COMMANDS = "ev-charger/{uuid}/commands"
GE_API_EVC_COMMAND_DATA = "ev-charger/{uuid}/commands/{command}"
GE_API_EVC_SEND_COMMAND = "ev-charger/{uuid}/commands/{command}"
GE_API_EVC_SESSIONS = "ev-charger/{uuid}/charging-sessions?start_time={start_time}&end_time={end_time}&pageSize=32"

GE_REGISTER_BATTERY_CUTOFF_LIMIT = 75

# 0	Current.Export	Instantaneous current flow from EV
# 1	Current.Import	Instantaneous current flow to EV
# 2	Current.Offered	Maximum current offered to EV
# 3	Energy.Active.Export.Register	Energy exported by EV (Wh or kWh)
# 4	Energy.Active.Import.Register	Energy imported by EV (Wh or kWh)
# 5	Energy.Reactive.Export.Register	Reactive energy exported by EV (varh or kvarh)
# 6	Energy.Reactive.Import.Register	Reactive energy imported by EV (varh or kvarh)
# 7	Energy.Active.Export.Interval	Energy exported by EV (Wh or kWh)
# 8	Energy.Active.Import.Interval	Energy imported by EV (Wh or kWh)
# 9	Energy.Reactive.Export.Interval	Reactive energy exported by EV. (varh or kvarh)
# 10 Energy.Reactive.Import.Interval	Reactive energy imported by EV. (varh or kvarh)
# 11 Frequency	Instantaneous reading of powerline frequency
# 12 Power.Active.Export	Instantaneous active power exported by EV. (W or kW)
# 13 Power.Active.Import	Instantaneous active power imported by EV. (W or kW)
# 14 Power.Factor	Instantaneous power factor of total energy flow
# 15 Power.Offered	Maximum power offered to EV
# 16 Power.Reactive.Export	Instantaneous reactive power exported by EV. (var or kvar)
# 17 Power.Reactive.Import	Instantaneous reactive power imported by EV. (var or kvar)
# 19 SoC	State of charge of charging vehicle in percentage
# 18 RPM	Fan speed in RPM
# 20 Temperature	Temperature reading inside Charge Point.
# 21 Voltage	Instantaneous AC RMS supply voltage
EVC_DATA_POINTS = {
    0: "Current.Export",
    1: "Current.Import",
    2: "Current.Offered",
    3: "Energy.Active.Export.Register",
    4: "Energy.Active.Import.Register",
    5: "Energy.Reactive.Export.Register",
    6: "Energy.Reactive.Import.Register",
    7: "Energy.Active.Export.Interval",
    8: "Energy.Active.Import.Interval",
    9: "Energy.Reactive.Export.Interval",
    10: "Energy.Reactive.Import.Interval",
    11: "Frequency",
    12: "Power.Active.Export",
    13: "Power.Active.Import",
    14: "Power.Factor",
    15: "Power.Offered",
    16: "Power.Reactive.Export",
    17: "Power.Reactive.Import",
    18: "RPM",
    19: "SoC",
    20: "Temperature",
    21: "Voltage",
}

# 0	EV Charger	These readings are taken by the EV charger internally
# 1	Grid Meter	These readings are taken by the EM115 meter monitoring the grid, if there is one installed
# 2	PV 1 Meter	These readings are taken by the EM115 meter monitoring PV generation source 1, if there is one installed
# 3	PV 2 Meter	These readings are taken by the EM115 meter monitoring PV generation source 2, if there is one installed
EVC_METER_CHARGER = 0
EVC_METER_GRID = 1
EVC_METER_PV1 = 2
EVC_METER_PV2 = 3

# Commands
# ['start-charge', 'stop-charge', 'adjust-charge-power-limit', 'set-plug-and-go', 'set-session-energy-limit', 'set-schedule', 'unlock-connector', 'delete-charging-profile', 'change-mode', 'restart-charger', 'change-randomised-delay-duration', 'add-id-tags', 'delete-id-tags', 'rename-id-tag', 'installation-mode', 'setup-version', 'set-active-schedule', 'set-max-import-capacity', 'enable-front-panel-led', 'configure-inverter-control', 'perform-factory-reset', 'configuration-mode', 'enable-local-control']
# Command adjust-charge-power-limit  {'min': 6, 'max': 32, 'value': 32, 'unit': 'A'}
# Command set-plug-and-go  {'value': False, 'disabled': False, 'message': None}
# Command {'min': 0.1, 'max': 250, 'value': None, 'unit': 'kWh'}
# Command set-schedule  {'schedules': []}
# Command unlock-connector  []
# Command delete-charging-profile data None response None
# Command change-mode  [{'active': False, 'available': True, 'image_path': '/images/dashboard/cards/ev/modes/eco-with-sun.png', 'title': 'Solar', 'key': 'SuperEco', 'description': 'Your vehicle will only charge when there is >1.4kW excess solar power available.'}, {'active': False, 'available': True, 'image_path': '/images/dashboard/cards/ev/modes/eco-with-sun-grid.png', 'title': 'Hybrid', 'key': 'Eco', 'description': 'Your vehicle will start charging using grid or solar at >1.4kW. As excess power becomes available, the charge rate will adjust automatically to maximise self consumption.'}, {'active': True, 'available': True, 'image_path': '/images/dashboard/cards/ev/modes/eco-with-grid.png', 'title': 'Grid', 'key': 'Boost', 'description': 'Your vehicle will charge using whichever power source is available up to the current limit you set.'}, {'active': False, 'available': False, 'image_path': '/images/dashboard/cards/ev/modes/eco-with-inverter.png', 'title': 'Inverter Control', 'key': 'ModbusSlave', 'description': 'Your vehicle will charge based upon instructions that it has been given by the GivEnergy Inverter.'}]
# Command restart-charger  []
# Command change-randomised-delay-duration  []
# Command add-id-tags  {'id_tags': [], 'maximum_id_tags': 200}
# Command delete-id-tags  []
# Command rename-id-tag  []
# Command installation-mode ct_meter
# Command setup-version 1
# Command set-active-schedule {'schedule': None}
# Command set-max-import-capacity {'value': '80', 'min': 40, 'max': 100}
# Command enable-front-panel-led {'value': True}
# Command configure-inverter-control {'inverter_battery_export_split': 0, 'max_battery_discharge_power_to_evc': 0, 'mode': 'SuperEco'}
# Command perform-factory-reset []
# Command configuration-mode {'value': 'C'}
# Command enable-local-control {'value': True}
EVC_COMMAND_NAMES = {
    "start-charge": "Start Charge",
    "stop-charge": "Stop Charge",
    "adjust-charge-power-limit": "Adjust Charge Power Limit",
    "set-plug-and-go": "Set Plug and Go",
    "set-session-energy-limit": "Set Session Energy Limit",
    "set-schedule": "Set Schedule",
    "unlock-connector": "Unlock Connector",
    "delete-charging-profile": "Delete Charging Profile",
    "change-mode": "Change Mode",
    "restart-charger": "Restart Charger",
    "change-randomised-delay-duration": "Change Randomised Delay Duration",
    "add-id-tags": "Add ID Tags",
    "delete-id-tags": "Delete ID Tags",
    "rename-id-tag": "Rename ID Tag",
    "installation-mode": "Installation Mode",
    "setup-version": "Setup Version",
    "set-active-schedule": "Set Active Schedule",
    "set-max-import-capacity": "Set Max Import Capacity",
    "enable-front-panel-led": "Enable Front Panel LED",
    "configure-inverter-control": "Configure Inverter Control",
    "perform-factory-reset": "Perform Factory Reset",
    "configuration-mode": "Configuration Mode",
    "enable-local-control": "Enable Local Control",
}
EVC_SELECT_VALUE_KEY = {
    "change-mode": "mode",
    "adjust-charge-power-limit": "limit",
    "set-session-energy-limit": "limit",
    "change-randomised-delay-duration": "delay",
    "set-plug-and-go": "enabled",
}

# Unsupported commands
EVC_BLACKLIST_COMMANDS = ["installation-mode", "perform-factory-reset", "rename-id-tag", "delete-id-tags", "change-randomised-delay-duration"]

TIMEOUT = 240
RETRIES = 5
MAX_THREADS = 2

attribute_table = {
    "time": {"friendly_name": "Time", "icon": "mdi:clock", "unit_of_measurement": "Time", "state_class": "timestamp"},
    "status": {"friendly_name": "Status", "icon": "mdi:alert", "unit_of_measurement": "Status"},
    "solar_power": {"friendly_name": "Solar Power", "icon": "mdi:solar-power", "unit_of_measurement": "W", "device_class": "power"},
    "consumption_power": {"friendly_name": "Consumption", "icon": "mdi:flash", "unit_of_measurement": "W", "device_class": "power"},
    "battery_power": {"friendly_name": "Battery Power", "icon": "mdi:battery", "unit_of_measurement": "W", "device_class": "power"},
    "battery_percent": {"friendly_name": "Battery Percent", "icon": "mdi:battery", "unit_of_measurement": "%", "device_class": "battery"},
    "battery_temperature": {"friendly_name": "Battery Temperature", "icon": "mdi:thermometer", "unit_of_measurement": "°C", "device_class": "temperature"},
    "grid_power": {"friendly_name": "Grid Power", "icon": "mdi:transmission-tower", "unit_of_measurement": "W", "device_class": "power"},
    "grid_voltage": {"friendly_name": "Grid Voltage", "icon": "mdi:transmission-tower", "unit_of_measurement": "V", "device_class": "voltage"},
    "grid_current": {"friendly_name": "Grid Current", "icon": "mdi:transmission-tower", "unit_of_measurement": "A", "device_class": "current"},
    "grid_frequency": {"friendly_name": "Grid Frequency", "icon": "mdi:transmission-tower", "unit_of_measurement": "Hz", "device_class": "frequency"},
    "solar_today": {"friendly_name": "Solar Today", "icon": "mdi:solar-power", "unit_of_measurement": "kWh", "device_class": "energy"},
    "consumption_today": {"friendly_name": "Consumption Today", "icon": "mdi:flash", "unit_of_measurement": "kWh", "device_class": "energy"},
    "battery_charge_today": {"friendly_name": "Battery Charge Today", "icon": "mdi:battery", "unit_of_measurement": "kWh", "device_class": "energy"},
    "battery_discharge_today": {"friendly_name": "Battery Discharge Today", "icon": "mdi:battery", "unit_of_measurement": "kWh", "device_class": "energy"},
    "grid_import_today": {"friendly_name": "Grid Import Today", "icon": "mdi:transmission-tower", "unit_of_measurement": "kWh", "device_class": "energy"},
    "grid_export_today": {"friendly_name": "Grid Export Today", "icon": "mdi:transmission-tower", "unit_of_measurement": "kWh", "device_class": "energy"},
    "solar_total": {"friendly_name": "Solar Total", "icon": "mdi:solar-power", "unit_of_measurement": "kWh", "device_class": "energy"},
    "consumption_total": {"friendly_name": "Consumption Total", "icon": "mdi:flash", "unit_of_measurement": "kWh", "device_class": "energy"},
    "battery_charge_total": {"friendly_name": "Battery Charge Total", "icon": "mdi:battery", "unit_of_measurement": "kWh", "device_class": "energy"},
    "battery_discharge_total": {"friendly_name": "Battery Discharge Total", "icon": "mdi:battery", "unit_of_measurement": "kWh", "device_class": "energy"},
    "grid_import_total": {"friendly_name": "Grid Import Total", "icon": "mdi:transmission-tower", "unit_of_measurement": "kWh", "device_class": "energy"},
    "grid_export_total": {"friendly_name": "Grid Export Total", "icon": "mdi:transmission-tower", "unit_of_measurement": "kWh", "device_class": "energy"},
    "max_charge_rate": {"friendly_name": "Max Charge Rate", "icon": "mdi:battery", "unit_of_measurement": "W", "device_class": "power"},
    "battery_size": {"friendly_name": "Battery Size", "icon": "mdi:battery", "unit_of_measurement": "kWh", "device_class": "energy"},
    "battery_dod": {"friendly_name": "Battery Depth of Discharge", "icon": "mdi:battery", "unit_of_measurement": "*", "device_class": "battery"},
}

BASE_TIME = datetime.strptime("00:00", "%H:%M")
OPTIONS_TIME = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M")) for minute in range(0, 24 * 60, 1)]
OPTIONS_TIME_FULL = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M") + ":00") for minute in range(0, 24 * 60, 1)]


class GECloudDirect:
    def __init__(self, api_key, automatic, base):
        """
        Setup client
        """
        self.base = base
        self.log = base.log
        self.api_key = api_key
        self.automatic = automatic
        self.register_list = {}
        self.settings = {}
        self.status = {}
        self.meter = {}
        self.info = {}
        self.stop_cloud = False
        self.api_started = False
        self.register_entity_map = {}
        self.long_poll_active = False
        self.pending_writes = {}
        self.evc_device = {}
        self.evc_data = {}
        self.evc_sessions = {}

        # API request metrics for monitoring
        self.requests_total = 0
        self.failures_total = 0
        self.last_success_timestamp = None

    def wait_api_started(self):
        """
        Return if the API has started
        """
        self.log("GECloud: Waiting for API to start")
        count = 0
        while not self.api_started and count < 120:
            time.sleep(1)
            count += 1
        if not self.api_started:
            self.log("Warn: GECloud: API failed to start in required time")
            return False
        return True

    def is_alive(self):
        """
        Check if the API is alive
        """
        return self.api_started

    async def switch_event(self, entity_id, service):
        """
        Switch event
        """
        mapping = self.register_entity_map.get(entity_id, None)
        if mapping:
            device = mapping.get("device", None)
            key = mapping.get("key", None)
            if device and key:
                setting = self.settings.get(device, {}).get(key, None)
                if setting:
                    value = setting.get("value", None)
                    if not isinstance(value, bool):
                        value = value == "on"

                    new_value = value
                    if service == "turn_on":
                        new_value = True
                    elif service == "turn_off":
                        new_value = False
                    elif service == "toggle":
                        new_value = not value
                    validation_rules = setting.get("validation_rules", [])

                    result = await self.async_write_inverter_setting(device, key, new_value)

                    if result and ("value" in result):
                        setting["value"] = result["value"]
                        await self.publish_registers(device, self.settings[device], select_key=key)
                    else:
                        self.log("GECloud: Failed to write setting {} {} to {}".format(device, key, new_value))

    async def number_event(self, entity_id, value):
        """
        Number event
        """
        mapping = self.register_entity_map.get(entity_id, None)
        if mapping:
            device = mapping.get("device", None)
            key = mapping.get("key", None)
            if device and key:
                setting = self.settings.get(device, {}).get(key, None)
                if setting:
                    try:
                        new_value = float(value)
                    except (ValueError, TypeError):
                        self.log("GECloud: Failed to convert {} to float".format(value))
                        return
                    validation_rules = setting.get("validation_rules", [])
                    if validation_rules:
                        for validation_rule in validation_rules:
                            if validation_rule.startswith("between:"):
                                range_min, range_max = validation_rule.split(":")[1].split(",")
                                if new_value < float(range_min):
                                    new_value = float(range_min)
                                if new_value > float(range_max):
                                    new_value = float(range_max)

                    result = await self.async_write_inverter_setting(device, key, new_value)
                    if result and ("value" in result):
                        setting["value"] = result["value"]
                        await self.publish_registers(device, self.settings[device], select_key=key)
                    else:
                        self.log("GECloud: Failed to write setting {} {} to {}".format(device, key, new_value))

    async def select_event(self, entity_id, value):
        """
        Select event
        """
        mapping = self.register_entity_map.get(entity_id, None)
        if mapping:
            device = mapping.get("device", None)
            key = mapping.get("key", None)
            if device and key:
                setting = self.settings.get(device, {}).get(key, None)
                if setting:
                    new_value = value
                    validation_rules = setting.get("validation_rules", [])
                    validation = setting.get("validation", None)
                    options_text = None
                    options_values = None

                    if validation_rules:
                        for validation_rule in validation_rules:
                            if validation_rule.startswith("in:"):
                                options_values = validation_rule.split(":")[1].split(",")

                    if validation.startswith("Value must be one of:"):
                        pre, post = validation.split("(")
                        post = post.replace(")", "")
                        post = post.replace(", ", ",")
                        options_text = post.split(",")
                        if new_value not in options_text:
                            self.log("GECloud: Invalid option {} for setting {} {} valid values are {}".format(new_value, device, key, options_text))
                            return
                    elif options_values is not None:
                        if new_value not in options_values:
                            self.log("GECloud: Invalid option {} for setting {} {} valid values are {}".format(new_value, device, key, options_values))
                            return

                    is_time = mapping.get("time", False)
                    if is_time:
                        # We actually write as HH:MM
                        new_value = new_value[:5]

                    result = await self.async_write_inverter_setting(device, key, new_value)
                    if result and ("value" in result):
                        setting["value"] = result["value"]
                        await self.publish_registers(device, self.settings[device], select_key=key)
                    else:
                        self.log("GECloud: Failed to write setting {} {} to {}".format(device, key, new_value))

    async def publish_info(self, device, device_info):
        """
        Publish the device info

        {'serial': 'SA2243G277',
         'status': 'UNKNOWN',
         'last_online': '2025-02-09T17:46:32Z',
         'last_updated': '2025-02-09T17:46:32Z',
         'commission_date': '2022-12-14T00:00:00Z',
         'info': {'battery_type': 'LITHIUM', 'battery': {'nominal_capacity': 186, 'nominal_voltage': 51.2, 'depth_of_discharge': 1}, 'model': 'GIV-HY3.6', 'max_charge_rate': 2600},
         'warranty': {'type': 'Standard Legacy', 'expiry_date': '2027-12-14T00:00:00Z'},
         'firmware_version': {'ARM': 193, 'DSP': 190},
         'connections': {'batteries': [{'module_number': 1, 'serial': 'DF2228G115', 'firmware_version': 3017, 'capacity': {'full': 184.82, 'design': 186}, 'cell_count': 16, 'has_usb': True, 'nominal_voltage': 51.2}], 'meters': []},
         'flags': ['full-power-discharge-in-eco-mode']}
        """

        for key in device_info:
            entity_name = "sensor.predbat_gecloud_" + device
            entity_name = entity_name.lower()
            attributes = {}
            if key == "info":
                info = device_info[key]
                cap = info.get("battery", {}).get("nominal_capacity", None)
                volt = info.get("battery", {}).get("nominal_voltage", None)
                dod = info.get("battery", {}).get("depth_of_discharge", None)

                capacity = None
                if cap and volt:
                    try:
                        capacity = round(cap * volt / 1000.0, 2)
                    except (ValueError, TypeError):
                        pass

                max_charge_rate = info.get("max_charge_rate", 0)
                self.log("GECloud: Device {} battery capacity {} max charge rate {}".format(device, capacity, max_charge_rate))

                self.base.dashboard_item(entity_name + "_battery_size", capacity, attributes=attribute_table.get("battery_size", {}), app="gecloud")
                self.base.dashboard_item(entity_name + "_max_charge_rate", max_charge_rate, attributes=attribute_table.get("max_charge_rate", {}), app="gecloud")
                self.base.dashboard_item(entity_name + "_battery_dod", dod, attributes=attribute_table.get("_battery_dod", {}), app="gecloud")

    async def publish_evc_data(self, serial, evc_data):
        """
        Data passed in is a dictionary of measurands according to EVC_DATA_POINTS
        """
        for key in evc_data:
            entity_name = "sensor.predbat_gecloud_" + serial
            entity_name = entity_name.lower()
            measurand = EVC_DATA_POINTS.get(key, None)
            if measurand:
                state = evc_data[key]
                if measurand == "Current.Export":
                    self.base.dashboard_item(entity_name + "_evc_current_export", state=state, attributes={"friendly_name": "EV Charger Current Export", "icon": "mdi:ev-station", "unit_of_measurement": "A", "device_class": "current"}, app="gecloud")
                elif measurand == "Current.Import":
                    self.base.dashboard_item(entity_name + "_evc_current_import", state=state, attributes={"friendly_name": "EV Charger Current Import", "icon": "mdi:ev-station", "unit_of_measurement": "A", "device_class": "current"}, app="gecloud")
                elif measurand == "Current.Offered":
                    self.base.dashboard_item(entity_name + "_evc_current_offered", state=state, attributes={"friendly_name": "EV Charger Current Offered", "icon": "mdi:ev-station", "unit_of_measurement": "A", "device_class": "current"}, app="gecloud")
                elif measurand == "Energy.Active.Export.Register":
                    self.base.dashboard_item(
                        entity_name + "_evc_energy_active_export_register", state=state, attributes={"friendly_name": "EV Charger Total Export", "icon": "mdi:ev-station", "unit_of_measurement": "kWh", "device_class": "energy"}, app="gecloud"
                    )
                elif measurand == "Energy.Active.Import.Register":
                    self.base.dashboard_item(
                        entity_name + "_evc_energy_active_import_register", state=state, attributes={"friendly_name": "EV Charger Total Import", "icon": "mdi:ev-station", "unit_of_measurement": "kWh", "device_class": "energy"}, app="gecloud"
                    )
                elif measurand == "Frequency":
                    self.base.dashboard_item(entity_name + "_evc_frequency", state=state, attributes={"friendly_name": "EV Charger Frequency", "icon": "mdi:ev-station", "unit_of_measurement": "Hz", "device_class": "frequency"}, app="gecloud")
                elif measurand == "Power.Active.Export":
                    self.base.dashboard_item(entity_name + "_evc_power_active_export", state=state, attributes={"friendly_name": "EV Charger Export Power", "icon": "mdi:ev-station", "unit_of_measurement": "W", "device_class": "power"}, app="gecloud")
                elif measurand == "Power.Active.Import":
                    self.base.dashboard_item(entity_name + "_evc_power_active_import", state=state, attributes={"friendly_name": "EV Charger Import Power", "icon": "mdi:ev-station", "unit_of_measurement": "W", "device_class": "power"}, app="gecloud")
                elif measurand == "Power.Factor":
                    self.base.dashboard_item(entity_name + "_evc_power_factor", state=state, attributes={"friendly_name": "EV Charger Power Factor", "icon": "mdi:ev-station", "unit_of_measurement": "*", "device_class": "power_factor"}, app="gecloud")
                elif measurand == "Power.Offered":
                    self.base.dashboard_item(entity_name + "_evc_power_offered", state=state, attributes={"friendly_name": "EV Charger Power Offered", "icon": "mdi:ev-station", "unit_of_measurement": "W", "device_class": "power"}, app="gecloud")
                elif measurand == "SoC":
                    self.base.dashboard_item(entity_name + "_evc_soc", state=state, attributes={"friendly_name": "EV Charger State of Charge", "icon": "mdi:ev-station", "unit_of_measurement": "%", "device_class": "battery"}, app="gecloud")
                elif measurand == "Temperature":
                    self.base.dashboard_item(entity_name + "_evc_temperature", state=state, attributes={"friendly_name": "EV Charger Temperature", "icon": "mdi:ev-station", "unit_of_measurement": "°C", "device_class": "temperature"}, app="gecloud")
                elif measurand == "Voltage":
                    self.base.dashboard_item(entity_name + "_evc_voltage", state=state, attributes={"friendly_name": "EV Charger Voltage", "icon": "mdi:ev-station", "unit_of_measurement": "V", "device_class": "voltage"}, app="gecloud")
                elif measurand == "RPM":
                    self.base.dashboard_item(entity_name + "_evc_rpm", state=state, attributes={"friendly_name": "EV Charger Fan Speed", "icon": "mdi:ev-station", "unit_of_measurement": "RPM"}, app="gecloud")

    async def publish_status(self, device, status):
        """
        Publish the status

        Status {'time': '2025-02-09T15:00:03Z', 'status': 'Normal', 'solar':
               {'power': 131, 'arrays': [{'array': 1, 'voltage': 251.7, 'current': 0.3, 'power': 77},
               {'array': 2, 'voltage': 144.2, 'current': 0.3, 'power': 54}]},
               'grid': {'voltage': 237.1, 'current': 4.2, 'power': 151, 'frequency': 50.05},
               'battery': {'percent': 60, 'power': 902, 'temperature': 12},
               'inverter': {'temperature': 27.2, 'power': 1029, 'output_voltage': 237.8, 'output_frequency': 50.06, 'eps_power': 10}, 'consumption': 878}

        """

        for key in status:
            entity_name = "sensor.predbat_gecloud_" + device
            entity_name = entity_name.lower()
            attributes = {}
            if key == "time":
                self.base.dashboard_item(entity_name + "_time", state=status[key], attributes=attribute_table.get("time", {}), app="gecloud")
            elif key == "status":
                self.base.dashboard_item(entity_name + "_status", state=status[key], attributes=attribute_table.get("status", {}), app="gecloud")
            elif key == "solar":
                self.base.dashboard_item(entity_name + "_solar_power", state=status[key].get("power", 0), attributes=attribute_table.get("solar_power", {}), app="gecloud")
            elif key == "consumption":
                self.base.dashboard_item(entity_name + "_consumption_power", state=status[key], attributes=attribute_table.get("consumption_power", {}), app="gecloud")
            elif key == "battery":
                self.base.dashboard_item(entity_name + "_battery_power", state=status[key].get("power", 0), attributes=attribute_table.get("battery_power", {}), app="gecloud")
                self.base.dashboard_item(entity_name + "_battery_percent", state=status[key].get("percent", 0), attributes=attribute_table.get("battery_percent", {}), app="gecloud")
                self.base.dashboard_item(entity_name + "_battery_temperature", state=status[key].get("temperature", 0), attributes=attribute_table.get("battery_temperature", {}), app="gecloud")
            elif key == "grid":
                self.base.dashboard_item(entity_name + "_grid_power", state=status[key].get("power", 0), attributes=attribute_table.get("grid_power", {}), app="gecloud")
                self.base.dashboard_item(entity_name + "_grid_voltage", state=status[key].get("voltage", 0), attributes=attribute_table.get("grid_voltage", {}), app="gecloud")
                self.base.dashboard_item(entity_name + "_grid_current", state=status[key].get("current", 0), attributes=attribute_table.get("grid_current", {}), app="gecloud")
                self.base.dashboard_item(entity_name + "_grid_frequency", state=status[key].get("frequency", 0), attributes=attribute_table.get("grid_frequency", {}), app="gecloud")

    async def publish_meter(self, device, meter):
        """
        Publish the meter data

        {'time': '2025-02-09T15:20:10Z',
        'today':
            {'solar': 1.7,
             'grid': {'import': 36.9, 'export': 0.8},
             'battery': {'charge': 10.2, 'discharge': 5.4},
             'consumption': 32.4,
             'ac_charge': 10.6
             },
        'total':
            {'solar': 6539.5,
             'grid': {'import': 19508.4, 'export': 3230.3},
             'battery': {'charge': 7290.95, 'discharge': 7290.95},
             'consumption': 21566.6,
             'ac_charge': 6350.8},
        'is_metered': True}

        """
        for key in meter:
            if key == "today":
                for subkey in meter[key]:
                    entity_name = "sensor.predbat_gecloud_" + device
                    entity_name = entity_name.lower()
                    attributes = {}
                    if subkey == "solar":
                        self.base.dashboard_item(entity_name + "_solar_today", state=meter[key][subkey], attributes=attribute_table.get("solar_today", {}), app="gecloud")
                    elif subkey == "consumption":
                        self.base.dashboard_item(entity_name + "_consumption_today", state=meter[key][subkey], attributes=attribute_table.get("consumption_today", {}), app="gecloud")
                    elif subkey == "battery":
                        self.base.dashboard_item(entity_name + "_battery_charge_today", state=meter[key][subkey].get("charge", 0), attributes=attribute_table.get("battery_charge_today", {}), app="gecloud")
                        self.base.dashboard_item(entity_name + "_battery_discharge_today", state=meter[key][subkey].get("discharge", 0), attributes=attribute_table.get("battery_discharge_today", {}), app="gecloud")
                    elif subkey == "grid":
                        self.base.dashboard_item(entity_name + "_grid_import_today", state=meter[key][subkey].get("import", 0), attributes=attribute_table.get("grid_import_today", {}), app="gecloud")
                        self.base.dashboard_item(entity_name + "_grid_export_today", state=meter[key][subkey].get("export", 0), attributes=attribute_table.get("grid_export_today", {}), app="gecloud")
            elif key == "total":
                for subkey in meter[key]:
                    entity_name = "sensor.predbat_gecloud_" + device
                    entity_name = entity_name.lower()
                    attributes = {}
                    if subkey == "solar":
                        self.base.dashboard_item(entity_name + "_solar_total", state=meter[key][subkey], attributes=attribute_table.get("solar_total", {}), app="gecloud")
                    elif subkey == "consumption":
                        self.base.dashboard_item(entity_name + "_consumption_total", state=meter[key][subkey], attributes=attribute_table.get("consumption_total", {}), app="gecloud")
                    elif subkey == "battery":
                        self.base.dashboard_item(entity_name + "_battery_charge_total", state=meter[key][subkey].get("charge", 0), attributes=attribute_table.get("battery_charge_total", {}), app="gecloud")
                        self.base.dashboard_item(entity_name + "_battery_discharge_total", state=meter[key][subkey].get("discharge", 0), attributes=attribute_table.get("battery_discharge_total", {}), app="gecloud")
                    elif subkey == "grid":
                        self.base.dashboard_item(entity_name + "_grid_import_total", state=meter[key][subkey].get("import", 0), attributes=attribute_table.get("grid_import_total", {}), app="gecloud")
                        self.base.dashboard_item(entity_name + "_grid_export_total", state=meter[key][subkey].get("export", 0), attributes=attribute_table.get("grid_export_total", {}), app="gecloud")

    async def publish_registers(self, device, registers, select_key=None):
        """
        Publish the registers
        """
        for key in registers:
            if select_key and key != select_key:
                continue
            reg_name = registers[key].get("name", None)
            validation_rules = registers[key].get("validation_rules", None)
            validation = registers[key].get("validation", None)
            value = registers[key].get("value", None)
            ha_name = reg_name.lower().replace(" ", "_").replace("%", "percent")
            attributes = {}
            attributes["friendly_name"] = reg_name

            is_select_time = False
            is_select_options = False
            is_number = False
            is_switch = False
            options_text = []

            for validation_rule in validation_rules:
                if validation_rule.startswith("date_format:H:i"):
                    is_select_time = True
                    options_text = OPTIONS_TIME_FULL
                    if isinstance(value, str) and len(value) == 5:
                        value = value + ":00"
                    attributes["device_class"] = "time"
                    attributes["state_class"] = "measurement"

                if validation_rule.startswith("boolean"):
                    is_switch = True
                if validation_rule == "writeonly":
                    is_switch = True

                if validation_rule.startswith("in:"):
                    is_select_options = True
                    options_text = validation_rule.split(":")[1].split(",")
                    attributes["state_class"] = "measurement"

                if validation_rule.startswith("between:"):
                    is_number = True
                    range_min, range_max = validation_rule.split(":")[1].split(",")
                    attributes["min"] = range_min
                    attributes["max"] = range_max
                    attributes["state_class"] = "measurement"
                    if "%" in reg_name:
                        attributes["device_class"] = "battery"
                        attributes["unit_of_measurement"] = "%"
                    elif "_power_percent" in ha_name:
                        attributes["device_class"] = "power_factor"
                        attributes["unit_of_measurement"] = "%"
                    elif "_power" in ha_name:
                        attributes["device_class"] = "power"
                        attributes["unit_of_measurement"] = "W"

            if validation.startswith("Value must be one of:"):
                pre, post = validation.split("(")
                post = post.replace(")", "")
                post = post.replace(", ", ",")
                options_text = post.split(",")

            if is_select_time or is_select_options:
                entity_name = "select.predbat_gecloud_" + device
                entity_id = entity_name + "_" + ha_name
                entity_id = entity_id.lower()
                attributes["options"] = options_text
                self.base.dashboard_item(entity_id, state=value, attributes=attributes, app="gecloud")
                self.register_entity_map[entity_id] = {"device": device, "key": key, "time": is_select_time}
            elif is_number:
                entity_name = "number.predbat_gecloud_" + device
                entity_id = entity_name + "_" + ha_name
                entity_id = entity_id.lower()
                self.base.dashboard_item(entity_id, state=value, attributes=attributes, app="gecloud")
                self.register_entity_map[entity_id] = {"device": device, "key": key}
            elif is_switch:
                entity_name = "switch.predbat_gecloud_" + device
                entity_id = entity_name + "_" + ha_name
                entity_id = entity_id.lower()
                state = False
                if isinstance(value, str):
                    if value in ["on", "true", "True"]:
                        state = True
                elif isinstance(value, bool):
                    state = value
                self.base.dashboard_item(entity_id, state="on" if state else "off", attributes=attributes, app="gecloud")
                self.register_entity_map[entity_id] = {"device": device, "key": key}

    async def async_automatic_config(self, devices):
        """
        Automatically configure predbat using GE Cloud auto-detected devices.
        'devices' is a dict with keys:
          - "ems": the EMS device serial
          - "battery": list of battery inverter serials (for battery-specific sensors)
        """
        if not devices or not devices["battery"]:
            self.log("GECloud: No battery devices found, cannot configure")
            return

        batteries = devices["battery"]
        num_inverters = len(batteries)

        self.base.args["inverter_type"] = ["GEC" for _ in range(num_inverters)]
        self.base.args["num_inverters"] = num_inverters
        self.base.args["load_today"] = ["sensor.predbat_gecloud_" + device + "_consumption_today" for device in batteries]
        self.base.args["import_today"] = ["sensor.predbat_gecloud_" + device + "_grid_import_today" for device in batteries]
        self.base.args["export_today"] = ["sensor.predbat_gecloud_" + device + "_grid_export_today" for device in batteries]
        self.base.args["pv_today"] = ["sensor.predbat_gecloud_" + device + "_solar_today" for device in batteries]
        self.base.args["charge_rate"] = ["number.predbat_gecloud_" + device + "_battery_charge_power" for device in batteries]
        self.base.args["battery_rate_max"] = ["sensor.predbat_gecloud_" + device + "_max_charge_rate" for device in batteries]
        self.base.args["discharge_rate"] = ["number.predbat_gecloud_" + device + "_battery_discharge_power" for device in batteries]
        self.base.args["battery_power"] = ["sensor.predbat_gecloud_" + device + "_battery_power" for device in batteries]
        self.base.args["pv_power"] = ["sensor.predbat_gecloud_" + device + "_solar_power" for device in batteries]
        self.base.args["load_power"] = ["sensor.predbat_gecloud_" + device + "_consumption_power" for device in batteries]
        self.base.args["grid_power"] = ["sensor.predbat_gecloud_" + device + "_grid_power" for device in batteries]
        self.base.args["soc_percent"] = ["sensor.predbat_gecloud_" + device + "_battery_percent" for device in batteries]
        self.base.args["soc_max"] = ["sensor.predbat_gecloud_" + device + "_battery_size" for device in batteries]
        self.base.args["reserve"] = ["number.predbat_gecloud_" + device + "_battery_reserve_percent_limit" for device in batteries]
        self.base.args["inverter_time"] = ["sensor.predbat_gecloud_" + device + "_time" for device in batteries]
        self.base.args["charge_start_time"] = ["select.predbat_gecloud_" + device + "_ac_charge_1_start_time" for device in batteries]
        self.base.args["charge_end_time"] = ["select.predbat_gecloud_" + device + "_ac_charge_1_end_time" for device in batteries]
        self.base.args["charge_limit"] = ["number.predbat_gecloud_" + device + "_ac_charge_upper_percent_limit" for device in batteries]
        self.base.args["discharge_start_time"] = ["select.predbat_gecloud_" + device + "_dc_discharge_1_start_time" for device in batteries]
        self.base.args["discharge_end_time"] = ["select.predbat_gecloud_" + device + "_dc_discharge_1_end_time" for device in batteries]
        self.base.args["scheduled_charge_enable"] = ["switch.predbat_gecloud_" + device + "_ac_charge_enable" for device in batteries]
        self.base.args["scheduled_discharge_enable"] = ["switch.predbat_gecloud_" + device + "_enable_dc_discharge" for device in batteries]
        self.base.args["pause_mode"] = ["select.predbat_gecloud_" + device + "_pause_battery" for device in batteries]
        self.base.args["pause_start_time"] = ["select.predbat_gecloud_" + device + "_pause_battery_start_time" for device in batteries]
        self.base.args["pause_end_time"] = ["select.predbat_gecloud_" + device + "_pause_battery_end_time" for device in batteries]
        if "givtcp_rest" in self.base.args:
            del self.base.args["givtcp_rest"]
        self.base.args["ge_cloud_serial"] = batteries[0]

        # reconfigure for EMS
        if devices["ems"]:
            self.log("GECloud: EMS detected")
            ems = devices["ems"]
            self.base.args["inverter_type"] = ["GEE" for _ in range(num_inverters)]
            self.base.args["ge_cloud_serial"] = ems
            self.base.args["load_today"] = ["sensor.predbat_gecloud_" + ems + "_consumption_today"]
            self.base.args["import_today"] = ["sensor.predbat_gecloud_" + ems + "_grid_import_today"]
            self.base.args["export_today"] = ["sensor.predbat_gecloud_" + ems + "_grid_export_today"]
            self.base.args["pv_today"] = ["sensor.predbat_gecloud_" + ems + "_solar_today"]
            self.base.args["charge_start_time"] = ["select.predbat_gecloud_" + ems + "_charge_start_time_slot_1" for _ in range(num_inverters)]
            self.base.args["charge_end_time"] = ["select.predbat_gecloud_" + ems + "_charge_end_time_slot_1" for _ in range(num_inverters)]
            self.base.args["idle_start_time"] = ["select.predbat_gecloud_" + ems + "_discharge_start_time_slot_1" for _ in range(num_inverters)]
            self.base.args["idle_end_time"] = ["select.predbat_gecloud_" + ems + "_discharge_end_time_slot_1" for _ in range(num_inverters)]
            self.base.args["charge_limit"] = ["number.predbat_gecloud_" + ems + "_charge_soc_percent_limit_1" for _ in range(num_inverters)]
            self.base.args["discharge_start_time"] = ["select.predbat_gecloud_" + ems + "_export_start_time_slot_1" for _ in range(num_inverters)]
            self.base.args["discharge_end_time"] = ["select.predbat_gecloud_" + ems + "_export_end_time_slot_1" for _ in range(num_inverters)]

            # EMS Produces the data for all inverters
            self.base.args["battery_power"] = ["sensor.predbat_gecloud_" + ems + "_battery_power"] + [0 for _ in range(num_inverters - 1)]
            self.base.args["pv_power"] = ["sensor.predbat_gecloud_" + ems + "_solar_power"] + [0 for _ in range(num_inverters - 1)]
            self.base.args["load_power"] = ["sensor.predbat_gecloud_" + ems + "_consumption_power"] + [0 for _ in range(num_inverters - 1)]
            self.base.args["grid_power"] = ["sensor.predbat_gecloud_" + ems + "_grid_power"] + [0 for _ in range(num_inverters - 1)]

        self.log("GECloud: Automatic configuration complete")

    async def start(self):
        """
        Start the client
        """
        self.stop_cloud = False
        self.api_started = False
        self.polling_mode = True
        # Get devices using the modified auto-detection (returns dict)
        devices_dict = await self.async_get_devices()
        evc_devices_dict = await self.async_get_evc_devices()

        # Build a list of devices to poll:
        # Use all battery inverter serials and also add the EMS device if it's distinct.
        device_list = devices_dict["battery"][:]

        ems_device = None
        if devices_dict["ems"]:
            ems_device = devices_dict["ems"]
            self.polling_mode = False
            self.log("GECloud: Found EMS device {} and disabled polling on inverters".format(ems_device))
            if ems_device not in device_list:
                device_list.append(ems_device)

        evc_device_list = []
        for device in evc_devices_dict:
            uuid = device.get("uuid", None)
            device_name = device.get("alias", None)
            evc_device_list.append(uuid)

        self.log("GECloud: Starting up, found devices {} evc_devices {}".format(device_list, evc_device_list))
        for device in device_list:
            self.pending_writes[device] = []

        if self.automatic:
            await self.async_automatic_config(devices_dict)

        seconds = 0
        while not self.stop_cloud and not self.base.fatal_error:
            try:
                if seconds % 60 == 0:
                    for device in device_list:
                        self.status[device] = await self.async_get_inverter_status(device)
                        await self.publish_status(device, self.status[device])
                        self.meter[device] = await self.async_get_inverter_meter(device)
                        await self.publish_meter(device, self.meter[device])
                        self.info[device] = await self.async_get_device_info(device)
                        await self.publish_info(device, self.info[device])
                    for uuid in evc_device_list:
                        self.evc_device[uuid] = await self.async_get_evc_device(uuid)
                        serial = self.evc_device[uuid].get("serial_number", "unknown")
                        self.evc_data[uuid] = await self.async_get_evc_device_data(uuid)
                        self.evc_sessions[uuid] = await self.async_get_evc_sessions(uuid)
                        await self.publish_evc_data(serial, self.evc_data[uuid])
                if seconds % 300 == 0:
                    for device in device_list:
                        if seconds == 0 or self.polling_mode or (device == ems_device):
                            self.settings[device] = await self.async_get_inverter_settings(device, first=False, previous=self.settings.get(device, {}))
                            await self.publish_registers(device, self.settings[device])

            except Exception as e:
                self.log("Error: GECloud: Exception in main loop {}".format(e))

            # Clear pending writes
            for device in device_list:
                if device in self.pending_writes:
                    self.pending_writes[device] = []

            if not self.api_started:
                print("GECloud API Started")
                self.api_started = True
            await asyncio.sleep(5)
            seconds += 5

    async def stop(self):
        self.stop_cloud = True

    async def async_send_evc_command(self, uuid, command, params):
        """
        Send a command to the EVC
        """
        for retry in range(RETRIES):
            data = await self.async_get_inverter_data(
                GE_API_EVC_SEND_COMMAND,
                uuid=uuid,
                command=command,
                post=True,
                datain=params,
            )
            if data and "success" in data:
                if not data["success"]:
                    data = None
            if data:
                break
            await asyncio.sleep(1 * (retry + 1))
        if data is None:
            self.log("Error: GECloud: Failed to send EVC command {} params {}".format(command, params))
        return data

    async def async_read_inverter_setting(self, serial, setting_id):
        """
        Read a setting from the inverter

        Code	Description	Potentially Successful?
        -1	The device did not respond before the request timed out	Yes
        -2	The device is offline	No
        -3	The device does not exist or your account does not have access to the device	No
        -4	There were one or more validation errors. Additional information may be available	No
        -5	There was a server error	Yes
        -6	There was no response from the server the device was last connected to. This may be because the device has been offline for an extended period of time and the server has since been decommissioned	Yes
        -7	The device is currently locked and cannot be modified	No

        """
        if serial in self.pending_writes:
            for pending in self.pending_writes[serial]:
                if pending["setting_id"] == setting_id:
                    return {"value": pending["value"]}

        for retry in range(RETRIES):
            data = await self.async_get_inverter_data(GE_API_INVERTER_READ_SETTING, serial, setting_id, post=True)
            data_value = None
            if data:
                data_value = data.get("value", -1)
            if data and data_value in [-3, -4, -7]:
                data = None
            elif data and data_value in [-1, -2, -5, -6]:
                data = None
                # Inverter timeout, try to spread requests out
                await asyncio.sleep(random.random() * 2)
            if data:
                break
            await asyncio.sleep(1 * (retry + 1))
        if data is None:
            self.log("Warn: GECloud: Failed to read inverter setting id {}".format(setting_id))
        return data

    async def async_write_inverter_setting(self, serial, setting_id, value):
        """
        Write a setting to the inverter
        """
        for retry in range(RETRIES):
            data = await self.async_get_inverter_data(
                GE_API_INVERTER_WRITE_SETTING,
                serial,
                setting_id,
                post=True,
                datain={"value": str(value), "context": "homeassistant"},
            )
            if data and "success" in data:
                if not data["success"]:
                    data = None
            if data:
                self.pending_writes[serial].append({"setting_id": setting_id, "value": value})
                break
            await asyncio.sleep(1 * (retry + 1))
        if data is None:
            self.log("Warn: GECloud: Failed to write setting id {} value {}".format(setting_id, value))
        return data

    async def async_get_inverter_settings(self, serial, first=False, previous={}):
        """
        Get settings for account
        """
        if serial not in self.register_list:
            self.register_list[serial] = await self.async_get_inverter_data_retry(GE_API_INVERTER_SETTINGS, serial)

        results = previous.copy()

        if serial in self.register_list:
            # Async read for all the registers
            futures = []
            pending = []
            complete = []
            loop = asyncio.get_running_loop()

            # Create the read tasks
            for setting in self.register_list[serial]:
                sid = setting.get("id", None)
                name = setting.get("name", None)

                validation_rules = setting.get("validation_rules", None)
                validation = setting.get("validation", None)
                if sid and name:
                    if "writeonly" in validation_rules:
                        results[sid] = {
                            "name": name,
                            "value": False,
                            "validation_rules": validation_rules,
                            "validation": validation,
                        }
                    else:
                        future = {}
                        future["sid"] = sid
                        future["serial"] = serial
                        future["name"] = name
                        future["validation_rules"] = validation_rules
                        future["validation"] = validation
                        pending.append(future)

            # Perform all the reads in parallel
            while pending or futures:
                while len(futures) < MAX_THREADS and pending:
                    future = pending.pop(0)
                    if not first:
                        future["future"] = loop.create_task(self.async_read_inverter_setting(future["serial"], future["sid"]))
                    futures.append(future)
                if futures:
                    future = futures.pop(0)
                    if first:
                        future["data"] = None
                    else:
                        future["data"] = await future["future"]
                    future["future"] = None
                    complete.append(future)
                if not first:
                    await asyncio.sleep(0.2)

            # Wait for all the futures to complete and store results
            for future in complete:
                sid = future["sid"]
                name = future["name"]
                validation_rules = future["validation_rules"]
                validation = future["validation"]
                data = future["data"]
                if data and ("value" in data):
                    value = data["value"]
                else:
                    value = None

                if value is None and sid in results:
                    # Keep previous failure on read failure
                    pass
                else:
                    results[sid] = {
                        "name": name,
                        "value": value,
                        "validation_rules": validation_rules,
                        "validation": validation,
                    }
        return results

    async def async_get_smart_device_data(self, uuid):
        """
        Get smart device data points
        """
        data = await self.async_get_inverter_data_retry(GE_API_SMART_DEVICE_DATA, uuid=uuid)
        for point in data:
            self.log("Smart device point {}".format(point))
            return point
        return {}

    async def async_get_evc_sessions(self, uuid):
        """
        Get list of EVC sessions
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=24)
        start_time = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        data = await self.async_get_inverter_data_retry(GE_API_EVC_SESSIONS, uuid=uuid, start_time=start_time, end_time=end_time)
        if isinstance(data, list):
            return data
        return []

    async def async_get_evc_device_data(self, uuid):
        """
        Get smart device data points
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=10)
        start_time = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_time = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Request all measurands that we want to track
        measurands = "&".join([f"measurands[]={i}" for i in range(22)])  # All measurands 0-21
        data = await self.async_get_inverter_data_retry(GE_API_EVC_DEVICE_DATA, uuid=uuid, meter_ids=str(EVC_METER_CHARGER), start_time=start_time, end_time=end_time, measurands=measurands)
        result = {}
        if not data:
            return result

        # Handle the new API response format
        data_points = data.get("data", []) if isinstance(data, dict) else data

        # Get the latest measurements from the most recent timestamp
        if data_points:
            latest_point = data_points[-1]  # Get most recent data point
            meter_id = latest_point.get("meter_id", -1)
            if meter_id == EVC_METER_CHARGER:
                for measurement in latest_point.get("measurements", []):
                    measurand = measurement.get("measurand", None)
                    if (measurand is not None) and measurand in EVC_DATA_POINTS:
                        value = measurement.get("value", None)
                        unit = measurement.get("unit", None)
                        result[measurand] = value
        self.log("EVC device point {}".format(result))
        return result

    async def async_get_smart_device(self, uuid):
        """
        Get smart device
        """
        device = await self.async_get_inverter_data_retry(GE_API_SMART_DEVICE, uuid=uuid)
        self.log("Device {}".format(device))
        if device:
            uuid = device.get("uuid", None)
            other_data = device.get("other_data", {})
            alias = device.get("alias", None)
            local_key = other_data.get("local_key", None)
            asset_id = other_data.get("asset_id", None)
            hardware_id = other_data.get("hardware_id", None)
            return {
                "uuid": uuid,
                "alias": alias,
                "local_key": local_key,
                "asset_id": asset_id,
                "hardware_id": hardware_id,
            }
        return {}

    async def async_get_evc_commands(self, uuid):
        """
        Get EVC commands
        """
        command_info = {}
        commands = await self.async_get_inverter_data_retry(GE_API_EVC_COMMANDS, uuid=uuid)
        # Not desirable command
        for command_drop in EVC_BLACKLIST_COMMANDS:
            if command_drop in commands:
                commands.remove(command_drop)

        # Get command data
        for command in commands:
            command_data = await self.async_get_inverter_data_retry(GE_API_EVC_COMMAND_DATA, command=command, uuid=uuid)
            command_info[command] = command_data

        return command_info

    async def async_get_evc_device(self, uuid):
        """
        Get EVC device
        """
        device = await self.async_get_inverter_data_retry(GE_API_EVC_DEVICE, uuid=uuid)
        self.log("Device {}".format(device))
        if device:
            uuid = device.get("uuid", None)
            alias = device.get("alias", None)
            serial_number = device.get("serial_number", None)
            online = device.get("online", None)
            went_offline_at = device.get("went_offline_at", None)
            status = device.get("status", None)
            type = device.get("type", None)
            return {"uuid": uuid, "alias": alias, "serial_number": serial_number, "status": status, "online": online, "type": type, "went_offline_at": went_offline_at}
        return {}

    async def async_get_smart_devices(self):
        """
        Get list of smart devices
        """
        device_list = await self.async_get_inverter_data_retry(GE_API_SMART_DEVICES)
        devices = []
        if device_list is not None:
            for device in device_list:
                uuid = device.get("uuid", None)
                other_data = device.get("other_data", {})
                alias = device.get("alias", None)
                local_key = other_data.get("local_key", None)
                devices.append({"uuid": uuid, "alias": alias, "local_key": local_key})
        return devices

    async def async_get_evc_devices(self):
        """
        Get list of smart devices
        """
        device_list = await self.async_get_inverter_data_retry(GE_API_EVC_DEVICES)
        devices = []
        if device_list is not None:
            for device in device_list:
                uuid = device.get("uuid", None)
                other_data = device.get("other_data", {})
                alias = device.get("alias", None)
                devices.append({"uuid": uuid, "alias": alias})
        return devices

    async def async_get_device_info(self, serial):
        """
        Get the device info
        """
        device_list = await self.async_get_inverter_data_retry(GE_API_DEVICE_INFO)
        if device_list is not None:
            for device in device_list:
                inverter = device.get("inverter", None)
                if inverter:
                    this_serial = inverter.get("serial", None)
                    if this_serial and this_serial.lower() == serial.lower():
                        return inverter
        return {}

    async def async_get_devices(self):
        """
        Get list of inverters from GE Cloud.
        Returns a dict with:
          "ems": serial (lowercase) of the EMS device (model "Plant EMS") if found
          "battery": list of serials (lowercase) for battery inverters (devices with non-empty batteries)
        """
        device_list = await self.async_get_inverter_data_retry(GE_API_DEVICES)
        result = {"ems": None, "battery": []}
        if device_list is None:
            return result

        for device in device_list:
            self.log("GECloud: Found device {}".format(device))
            inverter = device.get("inverter", {})
            serial = inverter.get("serial", None)
            model = inverter.get("info", {}).get("model", "").lower()
            batteries = inverter.get("connections", {}).get("batteries", [])
            if serial:
                serial = serial.lower()
                if "plant ems" in model:
                    result["ems"] = serial
                elif batteries:
                    result["battery"].append(serial)
        return result

    async def async_get_inverter_status(self, serial):
        """
        Get basis status for inverter
        """
        return await self.async_get_inverter_data_retry(GE_API_INVERTER_STATUS, serial)

    async def async_get_inverter_meter(self, serial):
        """
        Get meter data for inverter
        """
        meter = await self.async_get_inverter_data_retry(GE_API_INVERTER_METER, serial)
        return meter

    async def async_get_inverter_data_retry(self, endpoint, serial="", setting_id="", post=False, datain=None, uuid="", meter_ids="", start_time="", end_time="", command="", measurands=""):
        """
        Retry API call
        """
        for retry in range(RETRIES):
            data = await self.async_get_inverter_data(endpoint, serial, setting_id, post, datain, uuid, meter_ids, start_time=start_time, end_time=end_time, command=command, measurands=measurands)
            if data is not None:
                break
            await asyncio.sleep(1 * (retry + 1))
        if data is None:
            self.log("Warn: GECloud: Failed to get data from {}".format(endpoint))
        return data

    async def async_get_inverter_data(self, endpoint, serial="", setting_id="", post=False, datain=None, uuid="", meter_ids="", start_time="", end_time="", command="", measurands=""):
        """
        Basic API call to GE Cloud
        """
        # Increment request counter
        self.requests_total += 1

        url = GE_API_URL + endpoint.format(inverter_serial_number=serial, setting_id=setting_id, uuid=uuid, start_time=start_time, end_time=end_time, meter_ids=meter_ids, command=command)

        # Add measurands parameters if provided (for EV charger endpoints)
        if measurands:
            url += f"&{measurands}"
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            if post:
                if datain:
                    response = await asyncio.to_thread(requests.post, url, headers=headers, json=datain, timeout=TIMEOUT)
                else:
                    response = await asyncio.to_thread(requests.post, url, headers=headers, timeout=TIMEOUT)
            else:
                response = await asyncio.to_thread(requests.get, url, headers=headers, timeout=TIMEOUT)
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

        # Check data
        if data and "data" in data:
            data = data["data"]
        else:
            data = None
        if response.status_code in [200, 201]:
            if data is None:
                data = {}
            self.last_success_timestamp = time.time()
            return data
        if response.status_code in [401, 403, 404, 422]:
            # Unauthorized
            self.failures_total += 1
            return {}
        if response.status_code == 429:
            # Rate limiting so wait up to 30 seconds
            self.failures_total += 1
            await asyncio.sleep(random.random() * 30)
        return None


class GECloud:
    def clean_url_cache(self, now_utc):
        """
        Clean up the GE Cloud cache
        """
        current_keys = list(self.ge_url_cache.keys())
        for url in current_keys[:]:
            stamp = self.ge_url_cache[url]["stamp"]
            age = now_utc - stamp
            if age.seconds > (24 * 60 * 60):
                del self.ge_url_cache[url]

    def get_ge_url(self, url, headers, now_utc, max_age_minutes=30):
        """
        Get data from GE Cloud
        """
        if url in self.ge_url_cache:
            stamp = self.ge_url_cache[url]["stamp"]
            pdata = self.ge_url_cache[url]["data"]
            age = now_utc - stamp
            if age.seconds < (max_age_minutes * 60):
                self.log("Return cached GE data for {} age {} minutes".format(url, dp1(age.seconds / 60)))
                return pdata

        self.log("Fetching {}".format(url))
        try:
            r = requests.get(url, headers=headers)
        except (requests.Timeout, requests.exceptions.ReadTimeout, requests.exceptions.RequestException, requests.exceptions.ConnectionError) as e:
            return {}

        try:
            data = r.json()
        except requests.exceptions.JSONDecodeError as e:
            return {}

        self.ge_url_cache[url] = {}
        self.ge_url_cache[url]["stamp"] = now_utc
        self.ge_url_cache[url]["data"] = data
        return data

    def download_ge_data(self, now_utc):
        """
        Download consumption data from GE Cloud
        """
        geserial = self.get_arg("ge_cloud_serial")
        gekey = self.args.get("ge_cloud_key", None)
        self.clean_url_cache(now_utc)

        if not geserial:
            self.log("Error: GE Cloud has been enabled but ge_cloud_serial is not set to your serial")
            self.record_status("Warn: GE Cloud has been enabled but ge_cloud_serial is not set to your serial", had_errors=True)
            return False
        if not gekey:
            self.log("Error: GE Cloud has been enabled but ge_cloud_key is not set to your appkey")
            self.record_status("Warn: GE Cloud has been enabled but ge_cloud_key is not set to your appkey", had_errors=True)
            return False

        headers = {"Authorization": "Bearer  " + gekey, "Content-Type": "application/json", "Accept": "application/json"}
        mdata = []
        days_prev_count = 0
        while days_prev_count <= self.max_days_previous:
            days_prev = self.max_days_previous - days_prev_count
            time_value = now_utc - timedelta(days=days_prev)
            datestr = time_value.strftime("%Y-%m-%d")
            url = "https://api.givenergy.cloud/v1/inverter/{}/data-points/{}?pageSize=4096".format(geserial, datestr)
            while url:
                data = self.get_ge_url(url, headers, now_utc, 30 if days_prev == 0 else 8 * 60)
                darray = data.get("data", None)
                if darray is None:
                    # If we are less than 8 hours into today then ignore errors for today as data may not be available yet
                    if days_prev == 0:
                        self.log("Info: No GE Cloud data available for today yet, continuing")
                        days_prev_count += 1
                        break
                    else:
                        self.log("Warn: Error downloading GE data from URL {}".format(url))
                        self.record_status("Warn: Error downloading GE data from cloud", debug=url)
                        continue

                for item in darray:
                    timestamp = item["time"]
                    consumption = item["total"]["consumption"]
                    dimport = item["total"]["grid"]["import"]
                    dexport = item["total"]["grid"]["export"]
                    dpv = item["total"]["solar"]

                    new_data = {}
                    new_data["last_updated"] = timestamp
                    new_data["consumption"] = consumption
                    new_data["import"] = dimport
                    new_data["export"] = dexport
                    new_data["pv"] = dpv
                    mdata.append(new_data)
                url = data["links"].get("next", None)
            days_prev_count += 1

        # Find how old the data is
        last_updated_time = now_utc
        if len(mdata) > 0:
            item = mdata[0]
            try:
                last_updated_time = str2time(item["last_updated"])
            except (ValueError, TypeError):
                pass

        age = now_utc - last_updated_time
        self.load_minutes_age = age.days
        self.load_minutes = self.minute_data(mdata, self.max_days_previous, now_utc, "consumption", "last_updated", backwards=True, smoothing=True, scale=self.load_scaling, clean_increment=True)
        self.import_today = self.minute_data(mdata, self.max_days_previous, now_utc, "import", "last_updated", backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True)
        self.export_today = self.minute_data(mdata, self.max_days_previous, now_utc, "export", "last_updated", backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True)
        self.pv_today = self.minute_data(mdata, self.max_days_previous, now_utc, "pv", "last_updated", backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True)

        self.load_minutes_now = self.load_minutes.get(0, 0) - self.load_minutes.get(self.minutes_now, 0)
        self.import_today_now = self.import_today.get(0, 0) - self.import_today.get(self.minutes_now, 0)
        self.export_today_now = self.export_today.get(0, 0) - self.export_today.get(self.minutes_now, 0)
        self.pv_today_now = self.pv_today.get(0, 0) - self.pv_today.get(self.minutes_now, 0)
        self.log("Downloaded {} datapoints from GE going back {} days".format(len(self.load_minutes), self.load_minutes_age))
        return True
