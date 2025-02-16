import requests
from datetime import timedelta
from datetime import datetime
from datetime import timezone
from utils import str2time, dp1
import requests
import json
import asyncio
import random

"""
GE Cloud data download
"""

GE_API_URL = "https://api.givenergy.cloud/v1/"
GE_API_INVERTER_STATUS = "inverter/{inverter_serial_number}/system-data/latest"
GE_API_INVERTER_METER = "inverter/{inverter_serial_number}/meter-data/latest"
GE_API_INVERTER_SETTINGS = "inverter/{inverter_serial_number}/settings"
GE_API_INVERTER_READ_SETTING = (
    "inverter/{inverter_serial_number}/settings/{setting_id}/read"
)
GE_API_INVERTER_WRITE_SETTING = (
    "inverter/{inverter_serial_number}/settings/{setting_id}/write"
)
GE_API_DEVICES = "communication-device"
GE_API_DEVICE_INFO = "communication-device"
GE_API_SMART_DEVICES = "smart-device"
GE_API_SMART_DEVICE = "smart-device/{uuid}"
GE_API_SMART_DEVICE_DATA = "smart-device/{uuid}/data"
GE_API_EVC_DEVICES = "ev-charger"
GE_API_EVC_DEVICE = "ev-charger/{uuid}"
GE_API_EVC_DEVICE_DATA = "ev-charger/{uuid}/meter-data?start_time={start_time}&end_time={end_time}&meter_ids[]={meter_ids}"
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
    "enable-local-control": "Enable Local Control"
}
EVC_SELECT_VALUE_KEY = {
    "change-mode" : "mode",
    "adjust-charge-power-limit" : "limit",
    "set-session-energy-limit" : "limit",
    "change-randomised-delay-duration" : "delay",
    "set-plug-and-go" : "enabled",
}

# Unsupported commands
EVC_BLACKLIST_COMMANDS = ["installation-mode", "perform-factory-reset", "rename-id-tag", "delete-id-tags", "change-randomised-delay-duration"]

TIMEOUT = 240
RETRIES = 5
MAX_THREADS = 2

attribute_table = {
    "time" : {"friendly_name": "Time", "icon": "mdi:clock", "unit_of_measurement": "Time", "state_class": "timestamp"},
    "status" : {"friendly_name": "Status", "icon": "mdi:alert", "unit_of_measurement": "Status"},
    "solar_power" : {"friendly_name": "Solar Power", "icon": "mdi:solar-power", "unit_of_measurement": "W", "device_class": "power"},
    "consumption_power" : {"friendly_name": "Consumption", "icon": "mdi:flash", "unit_of_measurement": "W", "device_class": "power"},
    "battery_power" : {"friendly_name": "Battery Power", "icon": "mdi:battery", "unit_of_measurement": "W", "device_class": "power"},
    "battery_percent" : {"friendly_name": "Battery Percent", "icon": "mdi:battery", "unit_of_measurement": "%", "device_class": "battery"},
    "battery_temperature" : {"friendly_name": "Battery Temperature", "icon": "mdi:thermometer", "unit_of_measurement": "°C", "device_class": "temperature"},
    "grid_power" : {"friendly_name": "Grid Power", "icon": "mdi:transmission-tower", "unit_of_measurement": "W", "device_class": "power"},
    "grid_voltage" : {"friendly_name": "Grid Voltage", "icon": "mdi:transmission-tower", "unit_of_measurement": "V", "device_class": "voltage"},
    "grid_current" : {"friendly_name": "Grid Current", "icon": "mdi:transmission-tower", "unit_of_measurement": "A", "device_class": "current"},
    "grid_frequency" : {"friendly_name": "Grid Frequency", "icon": "mdi:transmission-tower", "unit_of_measurement": "Hz", "device_class": "frequency"},
    "solar_today" : {"friendly_name": "Solar Today", "icon": "mdi:solar-power", "unit_of_measurement": "kWh", "device_class": "energy"},
    "consumption_today" : {"friendly_name": "Consumption Today", "icon": "mdi:flash", "unit_of_measurement": "kWh", "device_class": "energy"},
    "battery_charge_today" : {"friendly_name": "Battery Charge Today", "icon": "mdi:battery", "unit_of_measurement": "kWh", "device_class": "energy"},
    "battery_discharge_today" : {"friendly_name": "Battery Discharge Today", "icon": "mdi:battery", "unit_of_measurement": "kWh", "device_class": "energy"},
    "grid_import_today" : {"friendly_name": "Grid Import Today", "icon": "mdi:transmission-tower", "unit_of_measurement": "kWh", "device_class": "energy"},
    "grid_export_today" : {"friendly_name": "Grid Export Today", "icon": "mdi:transmission-tower", "unit_of_measurement": "kWh", "device_class": "energy"},
    "solar_total" : {"friendly_name": "Solar Total", "icon": "mdi:solar-power", "unit_of_measurement": "kWh", "device_class": "energy"},
    "consumption_total" : {"friendly_name": "Consumption Total", "icon": "mdi:flash", "unit_of_measurement": "kWh", "device_class": "energy"},
    "battery_charge_total" : {"friendly_name": "Battery Charge Total", "icon": "mdi:battery", "unit_of_measurement": "kWh", "device_class": "energy"},
    "battery_discharge_total" : {"friendly_name": "Battery Discharge Total", "icon": "mdi:battery", "unit_of_measurement": "kWh", "device_class": "energy"},
    "grid_import_total" : {"friendly_name": "Grid Import Total", "icon": "mdi:transmission-tower", "unit_of_measurement": "kWh", "device_class": "energy"},
    "grid_export_total" : {"friendly_name": "Grid Export Total", "icon": "mdi:transmission-tower", "unit_of_measurement": "kWh", "device_class": "energy"},
    "max_charge_rate" : {"friendly_name": "Max Charge Rate", "icon": "mdi:battery", "unit_of_measurement": "W", "device_class": "power"},
    "battery_size" : {"friendly_name": "Battery Size", "icon": "mdi:battery", "unit_of_measurement": "kWh", "device_class": "energy"},
}

BASE_TIME = datetime.strptime("00:00", "%H:%M")
OPTIONS_TIME = [
    ((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M"))
    for minute in range(0, 24 * 60, 1)
]
OPTIONS_TIME_FULL = [
    ((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M") + ":00")
    for minute in range(0, 24 * 60, 1)
]

class GECloudDirect:
    def __init__(self, base):
        """
        Setup client
        """
        self.base = base
        self.log = base.log
        self.api_key = self.base.args.get("ge_cloud_key", None)
        self.register_list = {}
        self.settings = {}
        self.status = {}
        self.meter = {}
        self.info = {}
        self.stop_cloud = False
        self.register_entity_map = {}

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

                    self.log("GECloud: Write setting {} {} to {}".format(device, key, new_value))
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

                    self.log("GECloud: Write setting {} {} to {}".format(device, key, new_value))
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
                    if validation_rules:
                        for validation_rule in validation_rules:
                            if validation_rule.startswith("in:"):
                                options_text = validation_rule.split(":")[1].split(",")
                                if new_value not in options_text:
                                    self.log("GECloud: Invalid option {} for setting {} {}".format(new_value, device, key))
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
            if key == 'info':
                info = device_info[key]
                cap = info.get("info", {}).get("battery", {}).get("nominal_capacity", None)
                volt = info.get("info", {}).get("battery", {}).get("nominal_voltage", None)

                capacity = None
                if cap and volt:
                    try:
                        capacity = round(cap * volt / 1000.0, 2)
                    except (ValueError, TypeError):
                        pass

                max_charge_rate = info.get('info', {}).get('max_charge_rate', 0)

                self.base.set_state_wrapper(entity_name + "_battery_size", capacity, attributes=attribute_table.get("battery_size", {}))
                self.base.set_state_wrapper(entity_name + "_max_charge_rate", capacity, attributes=attribute_table.get("max_charge_rate", {}))

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
            if key == 'time':
                self.base.set_state_wrapper(entity_name + "_time", state=status[key], attributes=attribute_table.get("time", {}))
            elif key == 'status':
                self.base.set_state_wrapper(entity_name + "_status", state=status[key], attributes=attribute_table.get("status", {}))
            elif key == 'solar':
                self.base.set_state_wrapper(entity_name + "_solar_power", state=status[key].get("power", 0), attributes=attribute_table.get("solar_power", {}))
            elif key == 'consumption':
                self.base.set_state_wrapper(entity_name + "_consumption_power", state=status[key], attributes=attribute_table.get("consumption_power", {}))
            elif key == 'battery':
                self.base.set_state_wrapper(entity_name + "_battery_power", state=status[key].get("power", 0), attributes=attribute_table.get("battery_power", {}))
                self.base.set_state_wrapper(entity_name + "_battery_percent", state=status[key].get("percent", 0), attributes=attribute_table.get("battery_percent", {}))
                self.base.set_state_wrapper(entity_name + "_battery_temperature", state=status[key].get("temperature", 0), attributes=attribute_table.get("battery_temperature", {}))
            elif key == 'grid':
                self.base.set_state_wrapper(entity_name + "_grid_power", state=status[key].get("power", 0), attributes=attribute_table.get("grid_power", {}))
                self.base.set_state_wrapper(entity_name + "_grid_voltage", state=status[key].get("voltage", 0), attributes=attribute_table.get("grid_voltage", {}))
                self.base.set_state_wrapper(entity_name + "_grid_current", state=status[key].get("current", 0), attributes=attribute_table.get("grid_current", {}))
                self.base.set_state_wrapper(entity_name + "_grid_frequency", state=status[key].get("frequency", 0), attributes=attribute_table.get("grid_frequency", {}))

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
            if key == 'today':
                for subkey in meter[key]:
                    entity_name = "sensor.predbat_gecloud_" + device
                    entity_name = entity_name.lower()
                    attributes = {}
                    if subkey == 'solar':
                        self.base.set_state_wrapper(entity_name + "_solar_today", state=meter[key][subkey], attributes=attribute_table.get("solar_today", {}))
                    elif subkey == 'consumption':
                        self.base.set_state_wrapper(entity_name + "_consumption_today", state=meter[key][subkey], attributes=attribute_table.get("consumption_today", {}))
                    elif subkey == 'battery':
                        self.base.set_state_wrapper(entity_name + "_battery_charge_today", state=meter[key][subkey].get("charge", 0), attributes=attribute_table.get("battery_charge_today", {}))
                        self.base.set_state_wrapper(entity_name + "_battery_discharge_today", state=meter[key][subkey].get("discharge", 0), attributes=attribute_table.get("battery_discharge_today", {}))
                    elif subkey == 'grid':
                        self.base.set_state_wrapper(entity_name + "_grid_import_today", state=meter[key][subkey].get("import", 0), attributes=attribute_table.get("grid_import_today", {}))
                        self.base.set_state_wrapper(entity_name + "_grid_export_today", state=meter[key][subkey].get("export", 0), attributes=attribute_table.get("grid_export_today", {}))
            elif key == 'total':
                for subkey in meter[key]:
                    entity_name = "sensor.predbat_gecloud_" + device
                    entity_name = entity_name.lower()
                    attributes = {}
                    if subkey == 'solar':
                        self.base.set_state_wrapper(entity_name + "_solar_total", state=meter[key][subkey], attributes=attribute_table.get("solar_total", {}))
                    elif subkey == 'consumption':
                        self.base.set_state_wrapper(entity_name + "_consumption_total", state=meter[key][subkey], attributes=attribute_table.get("consumption_total", {}))
                    elif subkey == 'battery':
                        self.base.set_state_wrapper(entity_name + "_battery_charge_total", state=meter[key][subkey].get("charge", 0), attributes=attribute_table.get("battery_charge_total", {}))
                        self.base.set_state_wrapper(entity_name + "_battery_discharge_total", state=meter[key][subkey].get("discharge", 0), attributes=attribute_table.get("battery_discharge_total", {}))
                    elif subkey == 'grid':
                        self.base.set_state_wrapper(entity_name + "_grid_import_total", state=meter[key][subkey].get("import", 0), attributes=attribute_table.get("grid_import_total", {}))
                        self.base.set_state_wrapper(entity_name + "_grid_export_total", state=meter[key][subkey].get("export", 0), attributes=attribute_table.get("grid_export_total", {}))

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
                self.base.set_state_wrapper(entity_id, state=value, attributes=attributes)
                self.register_entity_map[entity_id] = {"device": device, "key": key, "time" : is_select_time}
            elif is_number:
                entity_name = "number.predbat_gecloud_" + device
                entity_id = entity_name + "_" + ha_name
                entity_id = entity_id.lower()
                self.base.set_state_wrapper(entity_id, state=value, attributes=attributes)
                self.register_entity_map[entity_id] = {"device": device, "key": key}
            elif is_switch:
                entity_name = "switch.predbat_gecloud_" + device
                entity_id = entity_name + "_" + ha_name
                entity_id = entity_id.lower()
                self.base.set_state_wrapper(entity_id, state="on" if value else "off", attributes=attributes)
                self.register_entity_map[entity_id] = {"device": device, "key": key}

    async def start(self):
        """
        Start the client
        """
        self.stop_cloud = False
        self.devices = await self.async_get_devices()
        self.log("GECloud: Starting up, found devices {}".format(self.devices))

        seconds = 0
        while not self.stop_cloud and not self.base.fatal_error:
            try:
                if seconds % 60 == 0:
                    for device in self.devices:
                        self.status[device] = await self.async_get_inverter_status(device)
                        await self.publish_status(device, self.status[device])
                        self.meter[device] = await self.async_get_inverter_meter(device)
                        await self.publish_meter(device, self.meter[device])
                        self.info[device] = await self.async_get_device_info(device)
                        await self.publish_info(device, self.info[device])
                if seconds % 300 == 0:
                    for device in self.devices:
                        self.settings[device] = await self.async_get_inverter_settings(device, first=False, previous=self.settings.get(device, {}))
                        await self.publish_registers(device, self.settings[device])
            except Exception as e:
                self.log("Error: GECloud: Exception in main loop {}".format(e))

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
            self.log(
                "Write EVC command {} params {} returns {}".format(
                    command, params, data
                )
            )
            if data and "success" in data:
                if not data["success"]:
                    data = None
            if data:
                break
            await asyncio.sleep(1 * (retry + 1))
        if data is None:
            self.log(
                "Error: GECloud: Failed to send EVC command {} params {}".format(command, params)
            )
        return data

    async def async_read_inverter_setting(self, serial, setting_id):
        """
        Read a setting from the inverter
        """
        for retry in range(RETRIES):
            data = await self.async_get_inverter_data(
                GE_API_INVERTER_READ_SETTING, serial, setting_id, post=True
            )
            # -1 is a bad value
            if data and data.get("value", -1) == -1:
                data = None
            elif data and data.get("value", -1) == -2:
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
                break
            await asyncio.sleep(1 * (retry + 1))
        if data is None:
            self.log(
                "Warn: GECloud: Failed to write setting id {} value {}".format(setting_id, value)
            )
        return data

    async def async_get_inverter_settings(self, serial, first=False, previous={}):
        """
        Get settings for account
        """
        if serial not in self.register_list:
            self.register_list[serial] = await self.async_get_inverter_data_retry(
                GE_API_INVERTER_SETTINGS, serial
            )
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
                        future["future"] = loop.create_task(
                            self.async_read_inverter_setting(
                                future["serial"], future["sid"]
                            )
                        )
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
        data = await self.async_get_inverter_data_retry(
            GE_API_SMART_DEVICE_DATA, uuid=uuid
        )
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
        start_time=start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_time=now.strftime("%Y-%m-%dT%H:%M:%SZ")

        data = await self.async_get_inverter_data_retry(GE_API_EVC_SESSIONS, uuid=uuid, start_time=start_time, end_time=end_time)
        if isinstance(data, list):
            return data
        return None

    async def async_get_evc_device_data(self, uuid):
        """
        Get smart device data points
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=10)
        start_time=start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_time=now.strftime("%Y-%m-%dT%H:%M:%SZ")

        data = await self.async_get_inverter_data_retry(
            GE_API_EVC_DEVICE_DATA, uuid=uuid, meter_ids=str(EVC_METER_CHARGER), start_time=start_time, end_time=end_time
        )
        result = {}
        if not data:
            return result

        for meter in data:
            meter_id = meter.get("meter_id", -1)
            if meter_id == EVC_METER_CHARGER:
                for point in meter.get("measurements", []):
                    measurand = point.get("measurand", None)
                    if (measurand is not None) and measurand in EVC_DATA_POINTS:
                        value = point.get("value", None)
                        unit = point.get("unit", None)
                        result[EVC_DATA_POINTS[measurand]] = value
        self.log("EVC device point {}".format(result))
        return result

    async def async_get_smart_device(self, uuid):
        """
        Get smart device
        """
        device = await self.async_get_inverter_data_retry(
            GE_API_SMART_DEVICE, uuid=uuid
        )
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
            return {
                "uuid": uuid,
                "alias": alias,
                "serial_number": serial_number,
                "status": status,
                "online": online,
                "type": type,
                "went_offline_at": went_offline_at
            }
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
                devices.append(
                    {"uuid": uuid, "alias": alias}
                )
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
                    if this_serial and this_serial == serial:
                        return inverter
        return {}

    async def async_get_devices(self):
        """
        Get list of inverters
        """
        device_list = await self.async_get_inverter_data_retry(GE_API_DEVICES)
        serials = []
        if device_list is not None:
            for device in device_list:
                inverter = device.get("inverter", None)
                if inverter:
                    serial = inverter.get("serial", None)
                    if serial:
                        serials.append(serial)

        return serials

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

    async def async_get_inverter_data_retry(
        self, endpoint, serial="", setting_id="", post=False, datain=None, uuid="", meter_ids="", start_time="", end_time="", command=""
    ):
        """
        Retry API call
        """
        for retry in range(RETRIES):
            data = await self.async_get_inverter_data(
                endpoint, serial, setting_id, post, datain, uuid, meter_ids, start_time=start_time, end_time=end_time, command=command
            )
            if data is not None:
                break
            await asyncio.sleep(1 * (retry + 1))
        if data is None:
            self.log("Warn: GECloud: Failed to get data from {}".format(endpoint))
        return data

    async def async_get_inverter_data(
        self, endpoint, serial="", setting_id="", post=False, datain=None, uuid="", meter_ids="", start_time="", end_time="", command=""
    ):
        """
        Basic API call to GE Cloud
        """
        url = GE_API_URL + endpoint.format(
            inverter_serial_number=serial, setting_id=setting_id, uuid=uuid, start_time=start_time, end_time=end_time, meter_ids=meter_ids, command=command
        )
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if post:
            if datain:
                response = await asyncio.to_thread(
                    requests.post, url, headers=headers, json=datain, timeout=TIMEOUT
                )
            else:
                response = await asyncio.to_thread(
                    requests.post, url, headers=headers, timeout=TIMEOUT
                )
        else:
            response = await asyncio.to_thread(
                requests.get, url, headers=headers, timeout=TIMEOUT
            )
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
            return data
        if response.status_code in [401, 403, 404, 422]:
            # Unauthorized
            return {}
        if response.status_code == 429:
            # Rate limiting so wait up to 30 seconds
            await asyncio.sleep(random.random() * 30)
        return None


class GECloud:
    def get_ge_url(self, url, headers, now_utc):
        """
        Get data from GE Cloud
        """
        if url in self.ge_url_cache:
            stamp = self.ge_url_cache[url]["stamp"]
            pdata = self.ge_url_cache[url]["data"]
            age = now_utc - stamp
            if age.seconds < (30 * 60):
                self.log("Return cached GE data for {} age {} minutes".format(url, dp1(age.seconds / 60)))
                return pdata

        self.log("Fetching {}".format(url))
        r = requests.get(url, headers=headers)
        try:
            data = r.json()
        except requests.exceptions.JSONDecodeError:
            self.log("Warn: Error downloading GE data from URL {}".format(url))
            self.record_status("Warn: Error downloading GE data from cloud", debug=url, had_errors=True)
            return False

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
        days_prev = 0
        while days_prev <= self.max_days_previous:
            time_value = now_utc - timedelta(days=(self.max_days_previous - days_prev))
            datestr = time_value.strftime("%Y-%m-%d")
            url = "https://api.givenergy.cloud/v1/inverter/{}/data-points/{}?pageSize=4096".format(geserial, datestr)
            while url:
                data = self.get_ge_url(url, headers, now_utc)

                darray = data.get("data", None)
                if darray is None:
                    self.log("Warn: Error downloading GE data from URL {}".format(url))
                    self.record_status("Warn: Error downloading GE data from cloud", debug=url)
                    return False

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
            days_prev += 1

        # Find how old the data is
        item = mdata[0]
        try:
            last_updated_time = str2time(item["last_updated"])
        except (ValueError, TypeError):
            last_updated_time = now_utc

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
