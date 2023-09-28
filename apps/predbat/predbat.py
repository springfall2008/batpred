"""
Battery Prediction app
see Readme for information
"""
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from datetime import datetime, timedelta
import math
import re
import time
import pytz
import appdaemon.plugins.hass.hassapi as hass
import requests
import copy

THIS_VERSION = 'v7.4'
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_OCTOPUS = "%Y-%m-%d %H:%M:%S%z"
PREDICT_STEP = 5

SIMULATE = False         # Debug option, when set don't write to entities but simulate each 30 min period
SIMULATE_LENGTH = 23*60  # How many periods to simulate, set to 0 for just current
INVERTER_TEST = False    # Run inverter control self test

"""
Create an array of times
"""
OPTIONS_TIME = []
BASE_TIME = datetime.strptime("00:00:00", '%H:%M:%S')
for minute in range(0, 24*60, 5):
    timeobj = BASE_TIME + timedelta(seconds=minute*60)
    timestr = timeobj.strftime("%H:%M:%S")
    OPTIONS_TIME.append(timestr)

CONFIG_ITEMS = [
    {'name' : 'version',                       'friendly_name' : 'Predbat Core Update',            'type' : 'update', 'title' : 'Predbat', 'installed_version' : THIS_VERSION, 'release_url' : 'https://github.com/springfall2008/batpred/releases/tag/' + THIS_VERSION, 'entity_picture' : 'https://user-images.githubusercontent.com/48591903/249456079-e98a0720-d2cf-4b71-94ab-97fe09b3cee1.png'},
    {'name' : 'pv_metric10_weight',            'friendly_name' : 'Metric 10 Weight',               'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'fraction', 'icon' : 'mdi:percent'},
    {'name' : 'pv_scaling',                    'friendly_name' : 'PV Scaling',                     'type' : 'input_number', 'min' : 0,   'max' : 2.0,  'step' : 0.01, 'unit' : 'multiple', 'icon' : 'mdi:multiplication'},
    {'name' : 'load_scaling',                  'friendly_name' : 'Load Scaling',                   'type' : 'input_number', 'min' : 0,   'max' : 2.0,  'step' : 0.01, 'unit' : 'multiple', 'icon' : 'mdi:multiplication'},
    {'name' : 'battery_rate_max_scaling',      'friendly_name' : 'Battery rate max scaling',       'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'multiple', 'icon' : 'mdi:multiplication'},
    {'name' : 'battery_loss',                  'friendly_name' : 'Battery loss charge ',           'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'fraction', 'icon' : 'mdi:call-split'},
    {'name' : 'battery_loss_discharge',        'friendly_name' : 'Battery loss discharge',         'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'fraction', 'icon' : 'mdi:call-split'},
    {'name' : 'inverter_loss',                 'friendly_name' : 'Inverter Loss',                  'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'fraction', 'icon' : 'mdi:call-split'},
    {'name' : 'inverter_hybrid',               'friendly_name' : 'Inverter Hybrid',                'type' : 'switch'},
    {'name' : 'inverter_soc_reset',            'friendly_name' : 'Inverter SOC Reset',             'type' : 'switch'},
    {'name' : 'battery_capacity_nominal',      'friendly_name' : 'Use the Battery Capacity Nominal size', 'type' : 'switch'},
    {'name' : 'car_charging_energy_scale',     'friendly_name' : 'Car charging energy scale',      'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'fraction', 'icon' : 'mdi:multiplication'},
    {'name' : 'car_charging_threshold',        'friendly_name' : 'Car charging threshold',         'type' : 'input_number', 'min' : 4,   'max' : 8.5,  'step' : 0.10, 'unit' : 'kw', 'icon' : 'mdi:ev-station'},
    {'name' : 'car_charging_rate',             'friendly_name' : 'Car charging rate',              'type' : 'input_number', 'min' : 1,   'max' : 8.5,  'step' : 0.10, 'unit' : 'kw', 'icon' : 'mdi:ev-station'},
    {'name' : 'car_charging_loss',             'friendly_name' : 'Car charging loss',              'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'fraction', 'icon' : 'mdi:call-split'},
    {'name' : 'best_soc_margin',               'friendly_name' : 'Best SOC Margin',                'type' : 'input_number', 'min' : 0,   'max' : 30.0, 'step' : 0.10, 'unit' : 'kwh', 'icon' : 'mdi:battery-50'},
    {'name' : 'best_soc_min',                  'friendly_name' : 'Best SOC Min',                   'type' : 'input_number', 'min' : 0,   'max' : 30.0, 'step' : 0.10, 'unit' : 'kwh', 'icon' : 'mdi:battery-50'},
    {'name' : 'best_soc_max',                  'friendly_name' : 'Best SOC Max',                   'type' : 'input_number', 'min' : 0,   'max' : 30.0, 'step' : 0.10, 'unit' : 'kwh', 'icon' : 'mdi:battery-50'},
    {'name' : 'best_soc_keep',                 'friendly_name' : 'Best SOC Keep',                  'type' : 'input_number', 'min' : 0,   'max' : 30.0, 'step' : 0.10, 'unit' : 'kwh', 'icon' : 'mdi:battery-50'},
    {'name' : 'best_soc_step',                 'friendly_name' : 'Best SOC Step',                  'type' : 'input_number', 'min' : 0.1, 'max' : 1.0,  'step' : 0.05, 'unit' : 'kwh', 'icon' : 'mdi:battery-50'},
    {'name' : 'metric_min_improvement',        'friendly_name' : 'Metric Min Improvement',         'type' : 'input_number', 'min' : -50, 'max' : 50.0, 'step' : 0.1,  'unit' : 'p', 'icon' : 'mdi:currency-usd'},
    {'name' : 'metric_min_improvement_discharge', 'friendly_name' : 'Metric Min Improvement Discharge',    'type' : 'input_number', 'min' : -50, 'max' : 50.0, 'step' : 0.1,  'unit' : 'p', 'icon' : 'mdi:currency-usd'},
    {'name' : 'metric_battery_cycle',          'friendly_name' : 'Metric Battery Cycle Cost',      'type' : 'input_number', 'min' : -50, 'max' : 50.0, 'step' : 0.1,  'unit' : 'p/kwh', 'icon' : 'mdi:currency-usd'},
    {'name' : 'set_window_minutes',            'friendly_name' : 'Set Window Minutes',             'type' : 'input_number', 'min' : 5,   'max' : 720,  'step' : 5,    'unit' : 'minutes', 'icon' : 'mdi:timer-settings-outline'},
    {'name' : 'set_soc_minutes',               'friendly_name' : 'Set SOC Minutes',                'type' : 'input_number', 'min' : 5,   'max' : 720,  'step' : 5,    'unit' : 'minutes', 'icon' : 'mdi:timer-settings-outline'},
    {'name' : 'set_reserve_min',               'friendly_name' : 'Set Reserve Min',                'type' : 'input_number', 'min' : 4,   'max' : 100,  'step' : 1,    'unit' : '%',  'icon' : 'mdi:percent'},
    {'name' : 'rate_low_threshold',            'friendly_name' : 'Rate Low Threshold',             'type' : 'input_number', 'min' : 0.05,'max' : 0.95, 'step' : 0.05, 'unit' : 'multiple', 'icon' : 'mdi:multiplication'},
    {'name' : 'rate_high_threshold',           'friendly_name' : 'Rate High Threshold',            'type' : 'input_number', 'min' : 1.0, 'max' : 3.00, 'step' : 0.05, 'unit' : 'multiple', 'icon' : 'mdi:multiplication'},    
    {'name' : 'car_charging_hold',             'friendly_name' : 'Car charging hold',              'type' : 'switch'},
    {'name' : 'octopus_intelligent_charging',  'friendly_name' : 'Octopus Intelligent Charging',   'type' : 'switch'},
    {'name' : 'car_charging_plan_smart',       'friendly_name' : 'Car Charging Plan Smart',        'type' : 'switch'},
    {'name' : 'car_charging_from_battery',     'friendly_name' : 'Allow car to charge from battery', 'type' : 'switch'},
    {'name' : 'calculate_best',                'friendly_name' : 'Calculate Best',                 'type' : 'switch'},
    {'name' : 'calculate_best_charge',         'friendly_name' : 'Calculate Best Charge',          'type' : 'switch'},
    {'name' : 'calculate_best_discharge',      'friendly_name' : 'Calculate Best Disharge',        'type' : 'switch'},
    {'name' : 'calculate_discharge_first',     'friendly_name' : 'Calculate Discharge First',      'type' : 'switch'},
    {'name' : 'combine_charge_slots',          'friendly_name' : 'Combine Charge Slots',           'type' : 'switch'},
    {'name' : 'combine_discharge_slots',       'friendly_name' : 'Combine Discharge Slots',        'type' : 'switch'},
    {'name' : 'combine_mixed_rates',           'friendly_name' : 'Combined Mixed Rates',           'type' : 'switch'},
    {'name' : 'set_charge_window',             'friendly_name' : 'Set Charge Window',              'type' : 'switch'},
    {'name' : 'set_window_notify',             'friendly_name' : 'Set Window Notify',              'type' : 'switch'},
    {'name' : 'set_discharge_window',          'friendly_name' : 'Set Discharge Window',           'type' : 'switch'},
    {'name' : 'set_discharge_freeze',          'friendly_name' : 'Set Discharge Freeze',           'type' : 'switch'},
    {'name' : 'set_discharge_freeze_only',     'friendly_name' : 'Set Discharge Freeze Only',      'type' : 'switch'},
    {'name' : 'set_discharge_notify',          'friendly_name' : 'Set Discharge Notify',           'type' : 'switch'},
    {'name' : 'set_soc_enable',                'friendly_name' : 'Set Soc Enable',                 'type' : 'switch'},
    {'name' : 'set_soc_notify',                'friendly_name' : 'Set Soc Notify',                 'type' : 'switch'},
    {'name' : 'set_reserve_enable',            'friendly_name' : 'Set Reserve Enable',             'type' : 'switch'},
    {'name' : 'set_reserve_hold',              'friendly_name' : 'Set Reserve Hold',               'type' : 'switch'},
    {'name' : 'set_reserve_notify',            'friendly_name' : 'Set Reserve Notify',             'type' : 'switch'},
    {'name' : 'balance_inverters_enable',      'friendly_name' : 'Balance Inverters Enable (Experimental)', 'type' : 'switch'},
    {'name' : 'balance_inverters_charge',      'friendly_name' : 'Balance Inverters for charging',          'type' : 'switch'},
    {'name' : 'balance_inverters_discharge',   'friendly_name' : 'Balance Inverters for discharge',         'type' : 'switch'},
    {'name' : 'balance_inverters_crosscharge', 'friendly_name' : 'Balance Inverters for cross-charging',    'type' : 'switch'},
    {'name' : 'debug_enable',                  'friendly_name' : 'Debug Enable',                   'type' : 'switch', 'icon' : 'mdi:bug-outline'},
    {'name' : 'car_charging_plan_time',        'friendly_name' : 'Car charging planned ready time','type' : 'select', 'options' : OPTIONS_TIME, 'icon' : 'mdi:clock-end'},
    {'name' : 'rate_low_match_export',         'friendly_name' : 'Rate Low Match Export',          'type' : 'switch'},
    {'name' : 'load_filter_modal',             'friendly_name' : 'Apply modal filter historical load', 'type' : 'switch'},
    {'name' : 'iboost_enable',                 'friendly_name' : 'IBoost enable',                  'type' : 'switch'},
    {'name' : 'iboost_max_energy',             'friendly_name' : 'IBoost max energy',              'type' : 'input_number', 'min' : 0,   'max' : 5,     'step' : 0.1,  'unit' : 'kwh'},
    {'name' : 'iboost_today',                  'friendly_name' : 'IBoost today',                   'type' : 'input_number', 'min' : 0,   'max' : 5,     'step' : 0.1,  'unit' : 'kwh'},
    {'name' : 'iboost_max_power',              'friendly_name' : 'IBoost max power',               'type' : 'input_number', 'min' : 0,   'max' : 3500,  'step' : 100,  'unit' : 'w'},
    {'name' : 'iboost_min_power',              'friendly_name' : 'IBoost min power',               'type' : 'input_number', 'min' : 0,   'max' : 3500,  'step' : 100,  'unit' : 'w'},
    {'name' : 'iboost_min_soc',                'friendly_name' : 'IBoost min soc',                 'type' : 'input_number', 'min' : 0,   'max' : 100,   'step' : 5,    'unit' : '%', 'icon' : 'mdi:percent'},
    {'name' : 'holiday_days_left',             'friendly_name' : 'Holiday days left',              'type' : 'input_number', 'min' : 0,   'max' : 28,    'step' : 1,    'unit' : 'days', 'icon' : 'mdi:clock-end'},
]

class Inverter():
    def self_test(self):
        self.base.log("======= INVERTER CONTROL SELF TEST START - REST={} ========".format(self.rest_api))
        self.adjust_battery_target(99)
        self.adjust_battery_target(100)
        self.adjust_reserve(6)
        self.adjust_reserve(4)
        self.disable_charge_window()
        timea = datetime.strptime("23:00:00", "%H:%M:%S")
        timeb = datetime.strptime("23:01:00", "%H:%M:%S")
        timec = datetime.strptime("05:00:00", "%H:%M:%S")
        timed = datetime.strptime("05:01:00", "%H:%M:%S")
        self.adjust_charge_window(timeb, timed)
        self.adjust_charge_window(timea, timec)
        self.adjust_force_discharge(False, timec, timed)
        self.adjust_force_discharge(True, timea, timeb)
        self.adjust_force_discharge(False)
        self.base.log("======= INVERTER CONTROL SELF TEST END ========")

        if self.rest_api:
            self.rest_api = None
            self.rest_data = None
            self.self_test()
        exit

    def __init__(self, base, id=0):
        self.id = id
        self.base = base
        self.charge_enable_time = False
        self.charge_start_time_minutes = self.base.forecast_minutes 
        self.charge_start_end_minutes = self.base.forecast_minutes 
        self.charge_window = []
        self.discharge_window = []
        self.discharge_limits = []
        self.current_charge_limit = 0.0
        self.soc_kw = 0
        self.soc_percent = 0
        self.rest_data = None
        self.inverter_limit = 7500.0
        self.export_limit = 99999.0
        self.inverter_time = None
        self.reserve_percent = 4.0
        self.reserve_percent_current = 4.0
        self.battery_rate_max_raw = 0
        self.battery_rate_max_charge = 0
        self.battery_rate_max_discharge = 0
        self.battery_rate_max_charge_scaled = 0
        self.battery_rate_max_discharge_scaled = 0
        self.battery_power = 0

        # Rest API?
        self.rest_api = self.base.get_arg('givtcp_rest', None, indirect=False, index=self.id)
        if self.rest_api:
            self.base.log("Inverter {} using Rest API {}".format(self.id, self.rest_api))
            self.rest_data = self.rest_readData()

        # Battery size, charge and discharge rates
        ivtime = None
        if self.rest_data and ('Invertor_Details' in self.rest_data):
            idetails = self.rest_data['Invertor_Details']
            self.soc_max = float(idetails['Battery_Capacity_kWh'])
            self.nominal_capacity = self.soc_max
            if 'raw' in self.rest_data:
                self.nominal_capacity = float(self.rest_data['raw']['invertor']['battery_nominal_capacity']) / 19.53125  # XXX: Where does 19.53125 come from? I back calculated but why that number...
                if self.base.battery_capacity_nominal:
                    if abs(self.soc_max - self.nominal_capacity) > 1.0:
                       # XXX: Weird workaround for battery reporting wrong capacity issue
                       self.base.log("WARN: REST data reports Battery Capacity Kwh as {} but nominal indicates {} - using nominal".format(self.soc_max, self.nominal_capacity))
                    else:
                       self.base.log("REST data reports Battery Capacity Kwh as {} and nominal indicates {} - using nominal".format(self.soc_max, self.nominal_capacity))
                    self.soc_max = self.nominal_capacity
            self.soc_max *= self.base.battery_scaling
   
            # Max battery rate
            if 'Invertor_Max_Bat_Rate' in idetails:
                self.battery_rate_max_raw = idetails['Invertor_Max_Bat_Rate']
            elif 'Invertor_Max_Rate' in idetails:
                self.battery_rate_max_raw = idetails['Invertor_Max_Rate']
            else:
                self.battery_rate_max_raw = self.base.get_arg('charge_rate', attribute='max', index=self.id, default=2600.0)

            # Max invertor rate
            if 'Invertor_Max_Inv_Rate' in idetails:
                self.inverter_limit = idetails['Invertor_Max_Inv_Rate']
            
            # Inverter time
            if 'Invertor_Time' in idetails:
                ivtime = idetails['Invertor_Time']
        else:
            self.soc_max = self.base.get_arg('soc_max', default=10.0, index=self.id) * self.base.battery_scaling
            self.nominal_capacity = self.soc_max
            self.battery_rate_max_raw = self.base.get_arg('charge_rate', attribute='max', index=self.id, default=2600.0)
            ivtime = self.base.get_arg('inverter_time', index=self.id, default=None)
        
        # Battery rate max charge, discharge
        self.battery_rate_max_charge = min(self.base.get_arg('inverter_limit_charge', self.battery_rate_max_raw, index=self.id), self.battery_rate_max_raw) / 60.0 / 1000.0
        self.battery_rate_max_discharge = min(self.base.get_arg('inverter_limit_discharge', self.battery_rate_max_raw, index=self.id), self.battery_rate_max_raw) / 60.0 / 1000.0
        self.battery_rate_max_charge_scaled = self.battery_rate_max_charge * self.base.battery_rate_max_scaling
        self.battery_rate_max_discharge_scaled = self.battery_rate_max_discharge * self.base.battery_rate_max_scaling

        # Convert inverter time into timestamp
        if ivtime:
            try:
                self.inverter_time = datetime.strptime(ivtime, TIME_FORMAT)
            except (ValueError, TypeError):
                try:
                    self.inverter_time = datetime.strptime(ivtime, TIME_FORMAT_OCTOPUS)
                except (ValueError, TypeError):
                    self.base.log("Warn: Unable to read inverter time string {}".format(ivtime))
                    self.inverter_time = None

        # Check inverter time and confirm skew
        if self.inverter_time:
            tdiff = self.inverter_time - self.base.now_utc
            tdiff = self.base.dp2(tdiff.seconds / 60 + tdiff.days * 60*24)
            self.base.log("Invertor time {} AppDeamon time {} difference {} minutes".format(self.inverter_time, self.base.now_utc, tdiff))
            if abs(tdiff) >= 5:
                self.base.log("WARN: Invertor time is {} AppDeamon time {} this is {} minutes skewed, Predbat may not function correctly, please fix this by updating your inverter or fixing AppDeamon time zone".format(self.inverter_time, self.base.now_utc, tdiff))
                self.base.record_status("Invertor time is {} AppDeamon time {} this is {} minutes skewed, Predbat may not function correctly, please fix this by updating your inverter or fixing AppDeamon time zone".format(self.inverter_time, self.base.now_utc, tdiff), had_errors=True)

        # Get current reserve value
        if self.rest_data:
            self.reserve_percent_current = float(self.rest_data['Control']['Battery_Power_Reserve'])
        else:
            self.reserve_percent_current = max(self.base.get_arg('reserve', default=0.0, index=self.id), 4.0)
        self.reserve_current = self.base.dp2(self.soc_max * self.reserve_percent_current / 100.0)

        # Get the expected minimum reserve value
        if self.base.set_reserve_enable:
            self.reserve_percent = max(self.base.get_arg('set_reserve_min', 4.0), 4.0)
        else:
            self.reserve_percent  = self.reserve_percent_current
        self.reserve = self.base.dp2(self.soc_max * self.reserve_percent / 100.0)

        # Max inverter rate override
        if 'inverter_limit' in self.base.args:
            self.inverter_limit = self.base.get_arg('inverter_limit', self.inverter_limit, index=self.id) / (1000 * 60.0)
        if 'export_limit' in self.base.args:
            self.export_limit = self.base.get_arg('export_limit', self.inverter_limit, index=self.id) / (1000 * 60.0)
        # Can't export more than the inverter limit
        self.export_limit = min(self.export_limit, self.inverter_limit)

        # Log inveter details
        self.base.log("New Inverter {} with soc_max {} kWh nominal_capacity {} kWh battery rate raw {} w charge rate {} kw discharge rate {} kw ac limit {} kw export limit {} kw reserve {} % current_reserve {} %".format(self.id, self.base.dp2(self.soc_max), 
            self.base.dp2(self.nominal_capacity), self.base.dp2(self.battery_rate_max_raw), self.base.dp2(self.battery_rate_max_charge * 60.0), self.base.dp2(self.battery_rate_max_discharge * 60.0), self.base.dp2(self.inverter_limit*60), 
            self.base.dp2(self.export_limit*60), self.reserve_percent, self.reserve_percent_current))
        
    def update_status(self, minutes_now):
        """
        Update inverter status
        """
        if self.rest_api:
            self.rest_data = self.rest_readData()

        if self.rest_data:
            self.charge_enable_time = self.rest_data['Control']['Enable_Charge_Schedule'] == 'enable'
            self.discharge_enable_time = self.rest_data['Control']['Enable_Discharge_Schedule'] == 'enable'
            self.charge_rate_max = self.rest_data['Control']['Battery_Charge_Rate'] / 1000.0 / 60.0
            self.discharge_rate_max = self.rest_data['Control']['Battery_Discharge_Rate'] / 1000.0 / 60.0
        else:
            self.charge_enable_time = self.base.get_arg('scheduled_charge_enable', 'on', index=self.id) == 'on'
            self.discharge_enable_time = self.base.get_arg('scheduled_discharge_enable', 'off', index=self.id) == 'on'
            self.charge_rate_max = self.base.get_arg('charge_rate', index=self.id, default=2600.0) / 1000.0 / 60.0
            self.discharge_rate_max = self.base.get_arg('discharge_rate', index=self.id, default=2600.0) / 1000.0 / 60.0

        # Scale charge and discharge rates with battery scaling
        self.charge_rate_max *= self.base.battery_rate_max_scaling
        self.discharge_rate_max *= self.base.battery_rate_max_scaling

        if SIMULATE:
            self.soc_kw = self.base.sim_soc_kw
        else:
            if self.rest_data:
                self.soc_kw = self.rest_data['Power']['Power']['SOC_kWh'] * self.base.battery_scaling
            else:
                self.soc_kw = self.base.get_arg('soc_kw', default=0.0, index=self.id) * self.base.battery_scaling

        self.soc_percent = round((self.soc_kw / self.soc_max) * 100.0)

        if self.rest_data and ('Power' in self.rest_data):
            pdetails = self.rest_data['Power']
            if 'Power' in pdetails:
                self.battery_power = float(pdetails['Power']['Battery_Power'])
        else:
            self.battery_power = self.base.get_arg('battery_power', default=0.0, index=self.id)

        self.base.log("Inverter {} SOC: {} kw {} % Current charge rate {} w Current discharge rate {} wcurrent power {} w".format(self.id, self.base.dp2(self.soc_kw), self.soc_percent, self.charge_rate_max*60*1000, self.discharge_rate_max*60*1000.0, self.battery_power))

        # If the battery is being charged then find the charge window
        if self.charge_enable_time:
            # Find current charge window
            if SIMULATE:
                charge_start_time = datetime.strptime(self.base.sim_charge_start_time, "%H:%M:%S")
                charge_end_time = datetime.strptime(self.base.sim_charge_end_time, "%H:%M:%S")
            else:
                if self.rest_data:
                    charge_start_time = datetime.strptime(self.rest_data['Timeslots']['Charge_start_time_slot_1'], "%H:%M:%S")
                    charge_end_time = datetime.strptime(self.rest_data['Timeslots']['Charge_end_time_slot_1'], "%H:%M:%S")
                else:
                    charge_start_time = datetime.strptime(self.base.get_arg('charge_start_time', index=self.id), "%H:%M:%S")
                    charge_end_time = datetime.strptime(self.base.get_arg('charge_end_time', index=self.id), "%H:%M:%S")

            # Reverse clock skew
            charge_start_time -= timedelta(seconds=self.base.inverter_clock_skew_start * 60)
            charge_end_time -= timedelta(seconds=self.base.inverter_clock_skew_end * 60)

            # Compute charge window minutes start/end just for the next charge window
            self.charge_start_time_minutes = charge_start_time.hour * 60 + charge_start_time.minute
            self.charge_end_time_minutes = charge_end_time.hour * 60 + charge_end_time.minute

            if self.charge_end_time_minutes < self.charge_start_time_minutes:
                # As windows wrap, if end is in the future then move start back, otherwise forward
                if self.charge_end_time_minutes > minutes_now:
                    self.charge_start_time_minutes -= 60 * 24
                else:
                    self.charge_end_time_minutes += 60 * 24
        else:
            # If charging is disabled set a fake window outside
            self.charge_start_time_minutes = self.base.forecast_minutes
            self.charge_end_time_minutes = self.base.forecast_minutes

        # Construct charge window from the GivTCP settings
        self.charge_window = []

        self.base.log("Inverter {} scheduled charge enable is {}".format(self.id, self.charge_enable_time))
        if self.charge_enable_time:
            minute = max(0, self.charge_start_time_minutes)  # Max is here is start could be before midnight now
            minute_end = self.charge_end_time_minutes
            while minute < self.base.forecast_minutes:
                window = {}
                window['start'] = minute
                window['end']   = minute_end
                self.charge_window.append(window)
                minute += 24 * 60
                minute_end += 24 * 60

        self.base.log('Inverter {} charge windows currently {}'.format(self.id, self.charge_window))

        # Work out existing charge limits and percent
        if self.charge_enable_time:
            if self.rest_data:
                self.current_charge_limit = float(self.rest_data['Control']['Target_SOC'])
            else:
                self.current_charge_limit = self.base.get_arg('charge_limit', index=self.id, default=100.0)
        else:
            self.current_charge_limit = 0.0

        if self.charge_enable_time:
            self.base.log("Inverter {} Charge settings: {}-{} limit {} power {} kw".format(self.id, self.base.time_abs_str(self.charge_start_time_minutes), self.base.time_abs_str(self.charge_end_time_minutes), self.current_charge_limit, self.charge_rate_max * 60.0))
        else:
            self.base.log("Inverter {} Charge settings: timed charged is disabled, power {} kw".format(self.id, self.charge_rate_max * 60.0))
            
        # Construct discharge window from GivTCP settings
        self.discharge_window = []

        if self.rest_data:
            discharge_start = datetime.strptime(self.rest_data['Timeslots']['Discharge_start_time_slot_1'], "%H:%M:%S")
            discharge_end = datetime.strptime(self.rest_data['Timeslots']['Discharge_end_time_slot_1'], "%H:%M:%S")
        else:
            discharge_start = datetime.strptime(self.base.get_arg('discharge_start_time', index=self.id), "%H:%M:%S")
            discharge_end = datetime.strptime(self.base.get_arg('discharge_end_time', index=self.id), "%H:%M:%S")

        # Reverse clock skew
        discharge_start -= timedelta(seconds=self.base.inverter_clock_skew_discharge_start * 60)
        discharge_end -= timedelta(seconds=self.base.inverter_clock_skew_discharge_end * 60)

        # Compute discharge window minutes start/end just for the next discharge window
        self.discharge_start_time_minutes = discharge_start.hour * 60 + discharge_start.minute
        self.discharge_end_time_minutes = discharge_end.hour * 60 + discharge_end.minute

        if self.charge_end_time_minutes < self.charge_start_time_minutes:
            # As windows wrap, if end is in the future then move start back, otherwise forward
            if self.discharge_end_time_minutes > minutes_now:
                self.discharge_start_time_minutes -= 60 * 24
            else:
                self.discharge_end_time_minutes += 60 * 24
        
        self.base.log("Inverter {} scheduled discharge enable is {}".format(self.id, self.discharge_enable_time))
        # Pre-fill current discharge window
        # Store it even when discharge timed isn't enabled as it won't be outside the actual slot
        if True:
            minute = max(0, self.discharge_start_time_minutes)  # Max is here is start could be before midnight now
            minute_end = self.discharge_end_time_minutes
            while minute < self.base.forecast_minutes:
                window = {}
                window['start'] = minute
                window['end']   = minute_end
                self.discharge_window.append(window)
                minute += 24 * 60
                minute_end += 24 * 60

         # Pre-fill best discharge enables
        if self.discharge_enable_time:
            self.discharge_limits = [self.reserve_percent for i in range(0, len(self.discharge_window))]
        else:
            self.discharge_limits = [100.0 for i in range(0, len(self.discharge_window))]

        self.base.log('Inverter {} discharge windows currently {}'.format(self.id, self.discharge_window))

        if INVERTER_TEST:
            self.self_test()

    def adjust_reserve(self, reserve):
        """
        Adjust the reserve target % in GivTCP
        """
        
        if SIMULATE:
            current_reserve = float(self.base.sim_reserve)
        else:
            if self.rest_data:
                current_reserve = float(self.rest_data['Control']['Battery_Power_Reserve'])
            else:
                current_reserve = self.base.get_arg('reserve', index=self.id, default=0.0)

        # Clamp to minimum
        reserve = int(reserve)
        if reserve < self.reserve_percent:
            reserve = self.reserve_percent

        if current_reserve != reserve:
            self.base.log("Inverter {} Current Reserve is {} % and new target is {} %".format(self.id, current_reserve, reserve))
            if SIMULATE:
                self.base.sim_reserve = reserve
            else:
                if self.rest_api:
                    self.rest_setReserve(reserve)
                else:
                    entity_soc = self.base.get_entity(self.base.get_arg('reserve', indirect=False, index=self.id))
                    self.write_and_poll_value('reserve', entity_soc, reserve)
                if self.base.set_reserve_notify:
                    self.base.call_notify('Predbat: Inverter {} Target Reserve has been changed to {} at {}'.format(self.id, reserve, self.base.time_now_str()))
                self.base.record_status("Inverter {} set reserve to {}".format(self.id, reserve))
        else:
            self.base.log("Inverter {} Current reserve is {} already at target".format(self.id, current_reserve))

    def adjust_charge_rate(self, new_rate):
        """
        Adjust charging rate
        """
        new_rate = int(new_rate + 0.5)

        if SIMULATE:
            current_rate = self.base.sim_charge_rate_max
        else:
            if self.rest_data:
                current_rate = self.rest_data['Control']['Battery_Charge_Rate']
            else:
                current_rate = self.base.get_arg('charge_rate', index=self.id, default=2600.0)

        if current_rate != new_rate:
            self.base.log("Inverter {} current charge rate is {} and new target is {}".format(self.id, current_rate, new_rate))
            if SIMULATE:
                self.base.sim_charge_rate_max = new_rate
            else:
                if self.rest_api:
                    self.rest_setChargeRate(new_rate)
                else:
                    entity = self.base.get_entity(self.base.get_arg('charge_rate', indirect=False, index=self.id))
                    self.write_and_poll_value('charge_rate', entity, new_rate, fuzzy=100)
                if self.base.set_soc_notify:
                    self.base.call_notify('Predbat: Inverter {} charge rate changes to {} at {}'.format(self.id, new_rate, self.base.time_now_str()))
            self.base.record_status("Inverter {} charge rate changed to {}".format(self.id, new_rate))

    def adjust_discharge_rate(self, new_rate):
        """
        Adjust discharging rate
        """
        new_rate = int(new_rate + 0.5)

        if SIMULATE:
            current_rate = self.base.sim_discharge_rate_max
        else:
            if self.rest_data:
                current_rate = self.rest_data['Control']['Battery_Discharge_Rate']
            else:
                current_rate = self.base.get_arg('discharge_rate', index=self.id, default=2600.0)

        if current_rate != new_rate:
            self.base.log("Inverter {} current discharge rate is {} and new target is {}".format(self.id, current_rate, new_rate))
            if SIMULATE:
                self.base.sim_discharge_rate_max = new_rate
            else:
                if self.rest_api:
                    self.rest_setDischargeRate(new_rate)
                else:
                    entity = self.base.get_entity(self.base.get_arg('discharge_rate', indirect=False, index=self.id))
                    self.write_and_poll_value('discharge_rate', entity, new_rate, fuzzy=100)
                if self.base.set_discharge_notify:
                    self.base.call_notify('Predbat: Inverter {} discharge rate changes to {} at {}'.format(self.id, new_rate, self.base.time_now_str()))
            self.base.record_status("Inverter {} discharge rate changed to {}".format(self.id, new_rate))

    def adjust_battery_target(self, soc):
        """
        Adjust the battery charging target SOC % in GivTCP
        """

        # SOC has no decimal places
        soc = int(soc)

        # Check current setting and adjust
        if SIMULATE:
            current_soc = self.base.sim_soc
        else:
            if self.rest_data:
                current_soc = float(self.rest_data['Control']['Target_SOC'])
            else:
                current_soc = self.base.get_arg('charge_limit', index=self.id)

        if current_soc != soc:
            self.base.log("Inverter {} Current charge Limit is {} % and new target is {} %".format(self.id, current_soc, soc))
            if SIMULATE:
                self.base.sim_soc = soc
            else:
                if self.rest_api:
                    self.rest_setChargeTarget(soc)
                else:
                    entity_soc = self.base.get_entity(self.base.get_arg('charge_limit', indirect=False, index=self.id))
                    self.write_and_poll_value('charge_limit', entity_soc, soc)
    
                if self.base.set_soc_notify:
                    self.base.call_notify('Predbat: Inverter {} Target SOC has been changed to {} % at {}'.format(self.id, soc, self.base.time_now_str()))
            self.base.record_status("Inverter {} set soc to {}".format(self.id, soc))
        else:
            self.base.log("Inverter {} Current SOC is {} already at target".format(self.id, current_soc))

    def write_and_poll_switch(self, name, entity, new_value):
        """
        GivTCP Workaround, keep writing until correct
        """
        tries = 6
        for retry in range(0, 6):
            if new_value:
                entity.call_service('turn_on')
            else:
                entity.call_service('turn_off')
            time.sleep(10)
            old_value = entity.get_state()
            if isinstance(old_value, str):
                if old_value.lower() in ['on', 'enable', 'true']:
                    old_value = True
                else:
                    old_value = False
            if old_value == new_value:
                self.base.log("Inverter {} Wrote {} to {} successfully and got {}".format(self.id, name, new_value, entity.get_state()))
                return True
        self.base.log("WARN: Inverter {} Trying to write {} to {} didn't complete got {}".format(self.id, name, new_value, entity.get_state()))
        self.base.record_status("Warn - Inverter {} write to {} failed".format(self.id, name), had_errors=True)
        return False

    def write_and_poll_value(self, name, entity, new_value, fuzzy=0):
        """
        GivTCP Workaround, keep writing until correct
        """
        for retry in range(0, 6):
            entity.call_service("set_value", value=new_value)
            time.sleep(10)
            old_value = int(entity.get_state())
            if (abs(old_value - new_value) <= fuzzy):
                self.base.log("Inverter {} Wrote {} to {}, successfully now {}".format(self.id, name, new_value, int(entity.get_state())))
                return True
        self.base.log("WARN: Inverter {} Trying to write {} to {} didn't complete got {}".format(self.id, name, new_value, int(entity.get_state())))
        self.base.record_status("Warn - Inverter {} write to {} failed".format(self.id, name), had_errors=True)
        return False

    def write_and_poll_option(self, name, entity, new_value):
        """
        GivTCP Workaround, keep writing until correct
        """
        for retry in range(0, 6):
            entity.call_service("select_option", option=new_value)
            time.sleep(10)
            old_value = entity.get_state()
            if old_value == new_value:
                self.base.log("Inverter {} Wrote {} to {} successfully".format(self.id, name, new_value))
                return True
        self.base.log("WARN: Inverter {} Trying to write {} to {} didn't complete got {}".format(self.id, name, new_value, entity.get_state()))
        self.base.record_status("Warn - Inverter {} write to {} failed".format(self.id, name), had_errors=True)
        return False

    def adjust_inverter_mode(self, force_discharge, changed_start_end=False):
        """
        Adjust inverter mode between force discharge and ECO
        """
        if SIMULATE:
            old_inverter_mode = self.base.sim_inverter_mode
        else:
            if self.rest_data:
                old_inverter_mode = self.rest_data['Control']['Mode']
            else:
                # Inverter mode
                if changed_start_end and not self.rest_api:
                    # XXX: Workaround for GivTCP window state update time to take effort
                    self.base.log("Sleeping (workaround) as start/end of discharge window was just adjusted")
                    time.sleep(30)
                old_inverter_mode = self.base.get_arg('inverter_mode', index=self.id)

        # For the purpose of this function consider Eco Paused as the same as Eco (it's a difference in reserve setting)
        if old_inverter_mode == 'Eco (Paused)':
            old_inverter_mode = 'Eco'

        # Force discharge or Eco mode?
        if force_discharge:
            new_inverter_mode = 'Timed Export'
        else:
            new_inverter_mode = 'Eco'

        # Change inverter mode
        if old_inverter_mode != new_inverter_mode:
            if SIMULATE:
                self.base.sim_inverter_mode = new_inverter_mode
            else:
                if self.rest_api:
                    self.rest_setBatteryMode(new_inverter_mode)
                else:
                    entity = self.base.get_entity(self.base.get_arg('inverter_mode', indirect=False, index=self.id))
                    self.write_and_poll_option('inverter_mode', entity, new_inverter_mode)

                # Notify
                if self.base.set_discharge_notify:
                    self.base.call_notify("Predbat: Inverter {} Force discharge set to {} at time {}".format(self.id, force_discharge, self.base.time_now_str()))

            self.base.record_status("Inverter {} Set discharge mode to {}".format(self.id, new_inverter_mode))
            self.base.log("Inverter {} set force discharge to {}".format(self.id, force_discharge))

    def adjust_force_discharge(self, force_discharge, new_start_time=None, new_end_time=None):
        """
        Adjust force discharge on/off and set the time window correctly
        """
        if SIMULATE:
            old_start = self.base.sim_discharge_start
            old_end = self.base.sim_discharge_end
        else:
            if self.rest_data:
                old_start = self.rest_data['Timeslots']['Discharge_start_time_slot_1']
                old_end = self.rest_data['Timeslots']['Discharge_end_time_slot_1']
            else:
                old_start = self.base.get_arg('discharge_start_time', index=self.id)
                old_end = self.base.get_arg('discharge_end_time', index=self.id)

        # Start time to correct format
        if new_start_time:
            new_start_time += timedelta(seconds=self.base.inverter_clock_skew_discharge_start * 60)
            new_start = new_start_time.strftime("%H:%M:%S")
        else:
            new_start = None

        # End time to correct format
        if new_end_time:
            new_end_time += timedelta(seconds=self.base.inverter_clock_skew_discharge_end * 60)
            new_end = new_end_time.strftime("%H:%M:%S")
        else:
            new_end = None

        # Eco mode, turn it on before we change the discharge window
        if not force_discharge:
            self.adjust_inverter_mode(force_discharge)

        self.base.log("Inverter {} Adjust force discharge to {}, change times from {} - {} to {} - {}".format(self.id, force_discharge, new_start, new_end, old_start, old_end))
        changed_start_end = False

        # Change start time
        if new_start and new_start != old_start:
            self.base.log("Inverter {} set new start time to {}".format(self.id, new_start))
            if SIMULATE:
                self.base.sim_discharge_start = new_start
            else:
                if not self.rest_api:
                    changed_start_end = True
                    entity_discharge_start_time = self.base.get_entity(self.base.get_arg('discharge_start_time', indirect=False, index=self.id))
                    self.write_and_poll_option("discharge_start_time", entity_discharge_start_time, new_start)

        # Change end time
        if new_end and new_end != old_end:
            self.base.log("Inverter {} Set new end time to {} was {}".format(self.id, new_end, old_end))                    
            if SIMULATE:
                self.base.sim_discharge_end = new_end
            else:
                if not self.rest_api:
                    changed_start_end = True
                    entity_discharge_end_time = self.base.get_entity(self.base.get_arg('discharge_end_time', indirect=False, index=self.id))
                    self.write_and_poll_option("discharge_end_time", entity_discharge_end_time, new_end)
        
        # REST version of writing slot
        if self.rest_api and new_start and new_end and ((new_start != old_start) or (new_end != old_end)):
            changed_start_end = True
            if not SIMULATE:
                self.rest_setDischargeSlot1(new_start, new_end)

        # Force discharge, turn it on after we change the window
        if force_discharge:
            self.adjust_inverter_mode(force_discharge, changed_start_end=changed_start_end)

        # Notify
        if changed_start_end:
            self.base.record_status("Inverter {} set discharge slot to {} - {}".format(self.id, new_start, new_end))
            if self.base.set_discharge_notify:
                self.base.call_notify("Predbat: Inverter {} Discharge time slot set to {} - {} at time {}".format(self.id, new_start, new_end, self.base.time_now_str()))


    def disable_charge_window(self, notify=True):
        """
        Disable charge window
        """
        if SIMULATE:
            old_charge_schedule_enable = self.base.sim_charge_schedule_enable
        else:
            if self.rest_data:
                old_charge_schedule_enable = self.rest_data['Control']['Enable_Charge_Schedule']
            else:
                old_charge_schedule_enable = self.base.get_arg('scheduled_charge_enable', 'on', index=self.id)

        if old_charge_schedule_enable == 'on' or old_charge_schedule_enable == 'enable':
            if not SIMULATE:
                # Enable scheduled charge if not turned on
                if self.rest_api:
                    self.rest_enableChargeSchedule(False)
                else:
                    entity = self.base.get_entity(self.base.get_arg('scheduled_charge_enable', indirect=False, index=self.id))
                    self.write_and_poll_switch('scheduled_charge_enable', entity, False)
                if self.base.set_soc_notify and notify:
                    self.base.call_notify("Predbat: Inverter {} Disabled scheduled charging at {}".format(self.id, self.base.time_now_str()))
            else:
                self.base.sim_charge_schedule_enable = 'off'

            if notify:
                self.base.record_status("Inverter {} Turned off scheduled charge".format(self.id))
            self.base.log("Inverter {} Turning off scheduled charge".format(self.id))

        # Updated cached status to disabled    
        self.charge_enable_time = False
        self.charge_start_time_minutes = self.base.forecast_minutes
        self.charge_end_time_minutes = self.base.forecast_minutes

    def adjust_charge_window(self, charge_start_time, charge_end_time):
        """
        Adjust the charging window times (start and end) in GivTCP
        """
        if SIMULATE:
            old_start = self.base.sim_charge_start_time
            old_end = self.base.sim_charge_end_time
            old_charge_schedule_enable = self.base.sim_charge_schedule_enable
        else:
            if self.rest_data:
                old_start = self.rest_data['Timeslots']['Charge_start_time_slot_1']
                old_end = self.rest_data['Timeslots']['Charge_end_time_slot_1']
                old_charge_schedule_enable = self.rest_data['Control']['Enable_Charge_Schedule']
            else:
                old_start = self.base.get_arg('charge_start_time', index=self.id)
                old_end = self.base.get_arg('charge_end_time', index=self.id)
                old_charge_schedule_enable = self.base.get_arg('scheduled_charge_enable', 'on', index=self.id)

        # Apply clock skew
        charge_start_time += timedelta(seconds=self.base.inverter_clock_skew_start * 60)
        charge_end_time += timedelta(seconds=self.base.inverter_clock_skew_end * 60)

        # Convert to string
        new_start = charge_start_time.strftime("%H:%M:%S")
        new_end = charge_end_time.strftime("%H:%M:%S")

        self.base.log("Inverter {} charge window is {} - {}, being changed to {} - {}".format(self.id, old_start, old_end, new_start, new_end))

        # Disable scheduled charge during change of window to avoid a blip in charging if not required
        have_disabled = False
        if new_start != old_start or new_end != old_end:
            self.disable_charge_window(notify=False)
            have_disabled = True

        # Program start slot
        if new_start != old_start:
            if SIMULATE:
                self.base.sim_charge_start_time = new_start
                self.base.log("Simulate sim_charge_start_time now {}".format(new_start))
            else:
                if not self.rest_api:
                    entity_start = self.base.get_entity(self.base.get_arg('charge_start_time', indirect=False, index=self.id))
                    self.write_and_poll_option("charge_start_time", entity_start, new_start)

        # Program end slot
        if new_end != old_end:
            if SIMULATE:
                self.base.sim_charge_end_time = new_end
                self.base.log("Simulate sim_charge_end_time now {}".format(new_end))
            else:
                if not self.rest_api:
                    entity_end = self.base.get_entity(self.base.get_arg('charge_end_time', indirect=False, index=self.id))
                    self.write_and_poll_option("charge_end_time", entity_end, new_end)

        if new_start != old_start or new_end != old_end:
            if self.rest_api and not SIMULATE:
                self.rest_setChargeSlot1(new_start, new_end)
            if self.base.set_window_notify and not SIMULATE:
                self.base.call_notify("Predbat: Inverter {} Charge window change to: {} - {} at {}".format(self.id, new_start, new_end, self.base.time_now_str()))
            self.base.record_status("Inverter {} Charge window change to: {} - {}".format(self.id, new_start, new_end))
            self.base.log("Inverter {} Updated start and end charge window to {} - {} (old {} - {})".format(self.id, new_start, new_end, old_start, old_end))

        if old_charge_schedule_enable == 'off' or old_charge_schedule_enable == 'disable' or have_disabled:
            if not SIMULATE:
                # Enable scheduled charge if not turned on
                if self.rest_api:
                    self.rest_enableChargeSchedule(True)
                else:
                    entity = self.base.get_entity(self.base.get_arg('scheduled_charge_enable', indirect=False, index=self.id))
                    self.write_and_poll_switch('scheduled_charge_enable', entity, True)

                # Only notify if it's a real change and not a temporary one
                if old_charge_schedule_enable == 'off' or old_charge_schedule_enable == 'disable' and self.base.set_soc_notify:
                    self.base.call_notify("Predbat: Inverter {} Enabling scheduled charging at {}".format(self.id, self.base.time_now_str()))
            else:
                self.base.sim_charge_schedule_enable = 'on'

            self.charge_enable_time = True
            self.base.record_status("Inverter {} Turned on charge enable".format(self.id))

            if old_charge_schedule_enable == 'off' or old_charge_schedule_enable == 'disable':
                self.base.log("Inverter {} Turning on scheduled charge".format(self.id))

    def rest_readData(self):
        """
        Get inverter status
        """
        url = self.rest_api + '/readData'
        try:
            r = requests.get(url)
        except Exception as e:
            self.base.log("ERROR: Exception raised {}".format(e))
            r = None

        if r and (r.status_code == 200):
            json = r.json()
            if 'Control' in json:
                return json
            else:
                self.base.log("WARN: Inverter {} read bad REST data from {} - REST will be disabled".format(self.id, url))
                self.base.record_status("Inverter {} read bad REST data from {} - REST will be disabled".format(self.id, url), had_errors=True)
                return None
        else:
            self.base.log("WARN: Inverter {} unable to read REST data from {} - REST will be disabled".format(self.id, url))
            self.base.record_status("Inverter {} unable to read REST data from {} - REST will be disabled".format(self.id, url), had_errors=True)
            return None

    def rest_runAll(self):
        """
        Updated and get inverter status
        """
        url = self.rest_api + '/runAll'
        r = requests.get(url)
        if r.status_code == 200:
            return r.json()
        else:
            return None

    def rest_setChargeTarget(self, target):
        """
        Configure charge target % via REST
        """
        target = int(target)
        url = self.rest_api + '/setChargeTarget'
        data = {"chargeToPercent": target}
        for retry in range(0, 5):
            r = requests.post(url, json=data)
            time.sleep(10)
            self.rest_data = self.rest_runAll()
            if float(self.rest_data['Control']['Target_SOC']) == target:
                self.base.log("Inverter {} charge target {} via REST successful on retry {}".format(self.id, target, retry))
                return True

        self.base.log("WARN: Inverter {} charge target {} via REST failed".format(self.id, target))
        self.base.record_status("Warn - Inverter {} REST failed to setChargeTarget".format(self.id), had_errors=True)
        return False

    def rest_setChargeRate(self, rate):
        """
        Configure charge target % via REST
        """
        rate = int(rate)
        url = self.rest_api + '/setChargeRate'
        data = {"chargeRate": rate}
        for retry in range(0, 5):
            r = requests.post(url, json=data)
            time.sleep(10)
            self.rest_data = self.rest_runAll()
            new = self.rest_data['Control']['Battery_Charge_Rate']
            if abs(new - rate) <  100:
                self.base.log("Inverter {} set charge rate {} via REST succesfull on retry {}".format(self.id, rate, retry))
                return True

        self.base.log("WARN: Inverter {} set charge rate {} via REST failed got {}".format(self.id, rate, self.rest_data['Control']['Battery_Charge_Rate']))
        self.base.record_status("Warn - Inverter {} REST failed to setChargeRate".format(self.id), had_errors=True)
        return False

    def rest_setDischargeRate(self, rate):
        """
        Configure charge target % via REST
        """
        rate = int(rate)
        url = self.rest_api + '/setDischargeRate'
        data = {"dischargeRate": rate}
        for retry in range(0, 5):
            r = requests.post(url, json=data)
            time.sleep(10)
            self.rest_data = self.rest_runAll()
            new = self.rest_data['Control']['Battery_Discharge_Rate']
            if abs(new - rate) <  100:
                self.base.log("Inverter {} set discharge rate {} via REST succesfull on retry {}".format(self.id, rate, retry))
                return True

        self.base.log("WARN: Inverter {} set discharge rate {} via REST failed got {}".format(self.id, rate, self.rest_data['Control']['Battery_Discharge_Rate']))
        self.base.record_status("Warn - Inverter {} REST failed to setDischargeRate to {} got {}".format(self.id, rate, self.rest_data['Control']['Battery_Discharge_Rate']), had_errors=True)
        return False

    def rest_setBatteryMode(self, inverter_mode):
        """
        Configure invert mode via REST
        """
        url = self.rest_api + '/setBatteryMode'
        data = {"mode": inverter_mode}

        for retry in range(0, 5):
            r = requests.post(url, json=data)
            time.sleep(10)
            self.rest_data = self.rest_runAll()
            if inverter_mode == self.rest_data['Control']['Mode']:
                self.base.log("Set inverter {} mode {} via REST successful on retry {}".format(self.id, inverter_mode, retry))
                return True

        self.base.log("WARN: Set inverter {} mode {} via REST failed".format(self.id, inverter_mode))
        self.base.record_status("Warn - Inverter {} REST failed to setBatteryMode".format(self.id), had_errors=True)
        return False

    def rest_setReserve(self, target):
        """
        Configure reserve % via REST
        """
        target = int(target)
        url = self.rest_api + '/setBatteryReserve'
        data = {"reservePercent": target}
        for retry in range(0, 5):
            r = requests.post(url, json=data)
            time.sleep(10)
            self.rest_data = self.rest_runAll()
            if float(self.rest_data['Control']['Battery_Power_Reserve']) == target:
                self.base.log("Set inverter {} reserve {} via REST successful on retry {}".format(self.id, target, retry))
                return True

        self.base.log("WARN: Set inverter {} reserve {} via REST failed".format(self.id, target, retry))
        self.base.record_status("Warn - Inverter {} REST failed to setBatteryMode".format(self.id), had_errors=True)
        return False

    def rest_enableChargeSchedule(self, enable):
        """
        Configure reserve % via REST
        """
        url = self.rest_api + '/enableChargeSchedule'
        data = {"state": "enable" if enable else "disable"}

        for retry in range(0, 5):
            r = requests.post(url, json=data)
            time.sleep(10)
            self.rest_data = self.rest_runAll()
            new_value = self.rest_data['Control']['Enable_Charge_Schedule']
            if isinstance(new_value, str):
                if new_value.lower() in ['enable', 'on', 'true']:
                    new_value = True
                else:
                    new_value = False
            if new_value == enable:
                self.base.log("Set inverter {} charge schedule {} via REST successful on retry {}".format(self.id, enable, retry))
                return True

        self.base.log("WARN: Set inverter {} charge schedule {} via REST failed got {}".format(self.id, enable, self.rest_data['Control']['Enable_Charge_Schedule']))
        self.base.record_status("Warn - Inverter {} REST failed to enableChargeSchedule".format(self.id), had_errors=True)
        return False

    def rest_setChargeSlot1(self, start, finish):
        """
        Configure charge slot via REST
        """
        url = self.rest_api + '/setChargeSlot1'
        data = {"start" : start[:5], "finish" : finish[:5]}

        for retry in range(0, 5):
            r = requests.post(url, json=data)
            time.sleep(10)
            self.rest_data = self.rest_runAll()
            if self.rest_data['Timeslots']['Charge_start_time_slot_1'] == start and self.rest_data['Timeslots']['Charge_end_time_slot_1'] == finish:
                self.base.log("Inverter {} set charge slot 1 {} via REST successful after retry {}".format(self.id, data, retry))
                return True
        
        self.base.log("WARN: Inverter {} set charge slot 1 {} via REST failed".format(self.id, data))
        self.base.record_status("Warn - Inverter {} REST failed to setChargeSlot1".format(self.id), had_errors=True)
        return False

    def rest_setDischargeSlot1(self, start, finish):
        """
        Configure charge slot via REST
        """
        url = self.rest_api + '/setDischargeSlot1'
        data = {"start" : start[:5], "finish" : finish[:5]}

        for retry in range(0, 5):
            r = requests.post(url, json=data)
            time.sleep(10)
            self.rest_data = self.rest_runAll()
            if self.rest_data['Timeslots']['Discharge_start_time_slot_1'] == start and self.rest_data['Timeslots']['Discharge_end_time_slot_1'] == finish:
                self.base.log("Inverter {} Set discharge slot 1 {} via REST successful after retry {}".format(self.id, data, retry))
                return True

        self.base.log("WARN: Inverter {} Set discharge slot 1 {} via REST failed".format(self.id, data))
        self.base.record_status("Warn - Inverter {} REST failed to setDischargeSlot1".format(self.id), had_errors=True)
        return False

class PredBat(hass.Hass):
    """ 
    The battery prediction class itself 
    """

    def call_notify(self, message):
        """
        Send HA notifications
        """
        for device in self.notify_devices:
            self.call_service("notify/" + device, message=message)

    def resolve_arg(self, arg, value, default=None, indirect=True, combine=False, attribute=None, index=None):
        """
        Resolve argument templates and state instances
        """
        if isinstance(value, list) and (index is not None):
            if index < len(value):
                value = value[index]
            else:
                self.log("WARN: Out of range index {} within item {} value {}".format(index, arg, value))
                value = None
            index = None

        if index:
            self.log("WARN: Out of range index {} within item {} value {}".format(index, arg, value))

        # If we have a list of items get each and add them up or return them as a list
        if isinstance(value, list):
            if combine:
                final = 0
                for item in value:
                    got = self.resolve_arg(arg, item, default=default, indirect=True)
                    try:
                        final += float(got)
                    except (ValueError, TypeError):
                        self.log("WARN: Return bad value {} from {} arg {}".format(got, item, arg))
                        self.record_status("Warn - Return bad value {} from {} arg {}".format(got, item, arg), had_errors=True) 
                return final
            else:
                final = []
                for item in value:
                    item = self.resolve_arg(arg, item, default=default, indirect=indirect)
                    final.append(item)
                return final

        # Resolve templated data
        for repeat in range(0, 2):
            if isinstance(value, str) and '{' in value:
                try:
                    value = value.format(**self.args)
                except KeyError:
                    self.log("WARN: can not resolve {} value {}".format(arg, value))
                    self.record_status("Warn - can not resolve {} value {}".format(arg, value), had_errors=True)
                    value = default

        # Resolve indirect instance
        if indirect and isinstance(value, str) and '.' in value:
            ovalue = value
            if attribute:
                value = self.get_state(entity_id = value, default=default, attribute=attribute)
            else:
                value = self.get_state(entity_id = value, default=default)
        return value

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None):
        """
        Argument getter that can use HA state as well as fixed values
        """
        value = None

        # Get From HA config
        value = self.get_ha_config(arg)

        # Resolve locally if no HA config
        if value is None:
            value = self.args.get(arg, default)
            value = self.resolve_arg(arg, value, default=default, indirect=indirect, combine=combine, attribute=attribute, index=index)

        if isinstance(default, float):
            # Convert to float?
            try:
                value = float(value)
            except (ValueError, TypeError):
                self.log("WARN: Return bad float value {} from {} using default {}".format(value, arg, default))
                self.record_status("Warn - Return bad float value {} from {}".format(value, arg), had_errors=True)
                value = default
        elif isinstance(default, int) and not isinstance(default, bool):
            # Convert to int? 
            try:
                value = int(float(value))
            except (ValueError, TypeError):
                self.log("WARN: Return bad int value {} from {} using default {}".format(value, arg, default))
                self.record_status("Warn - Return bad int value {} from {}".format(value, arg), had_errors=True)
                value = default
        elif isinstance(default, bool) and isinstance(value, str):
            # Convert to Boolean
            if value.lower() in ['on', 'true', 'yes', 'enabled', 'enable', 'connected']:
                value = True
            else:
                value = False
        elif isinstance(default, list):
            # Convert to list?
            if not isinstance(value, list):
                value = [value]
                
        # Set to user config
        self.expose_config(arg, value)
        return value

    def get_ge_url(self, url, headers, now_utc):
        """
        Get data from GE Cloud
        """
        if url in self.ge_url_cache:
            stamp = self.ge_url_cache[url]['stamp']
            pdata = self.ge_url_cache[url]['data']
            age = now_utc - stamp
            if age.seconds < (30 * 60):
                self.log("Return cached GE data for {} age {} minutes".format(url, age.seconds / 60))
                return pdata

        self.log("Fetching {}".format(url))
        r = requests.get(url, headers=headers)
        try:
            data = r.json()       
        except requests.exceptions.JSONDecodeError:
            self.log("WARN: Error downloading GE data from url {}".format(url))
            self.record_status("Warn - Error downloading GE data from cloud", debug=url, had_errors=True)
            return False
        
        self.ge_url_cache[url] = {}
        self.ge_url_cache[url]['stamp'] = now_utc
        self.ge_url_cache[url]['data'] = data
        return data

    def download_ge_data(self, now_utc):
        """
        Download consumption data from GE Cloud
        """
        geserial = self.get_arg('ge_cloud_serial')
        gekey = self.args.get('ge_cloud_key', None)

        if not geserial:
            self.log("ERROR: GE Cloud has been enabled but ge_cloud_serial is not set to your serial")
            self.record_status("Warn - GE Cloud has been enabled but ge_cloud_serial is not set to your serial", had_errors=True)
            return False
        if not gekey:
            self.log("ERROR: GE Cloud has been enabled but ge_cloud_key is not set to your appkey")
            self.record_status("Warn - GE Cloud has been enabled but ge_cloud_key is not set to your appkey", had_errors=True)
            return False

        headers = {
            'Authorization': 'Bearer  ' + gekey,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        mdata = []
        days_prev = 0
        while days_prev <= self.max_days_previous:
            time_value = now_utc - timedelta(days=(self.max_days_previous - days_prev))
            datestr = time_value.strftime("%Y-%m-%d")
            url = "https://api.givenergy.cloud/v1/inverter/{}/data-points/{}?pageSize=1024".format(geserial, datestr)
            while url:
                data = self.get_ge_url(url, headers, now_utc)

                darray = data.get('data', None)
                if darray is None:
                    self.log("WARN: Error downloading GE data from url {}".format(url))
                    self.record_status("Warn - Error downloading GE data from cloud", debug=url)
                    return False

                for item in darray:
                    timestamp = item['time']
                    consumption = item['total']['consumption']
                    dimport = item['total']['grid']['import']
                    dexport = item['total']['grid']['export']
                    dpv = item['total']['solar']

                    new_data = {}
                    new_data['last_updated'] = timestamp
                    new_data['consumption'] = consumption
                    new_data['import'] = dimport
                    new_data['export'] = dexport
                    new_data['pv'] = dpv
                    mdata.append(new_data)
                url = data['links'].get('next', None)
            days_prev += 1
            
        # Find how old the data is
        item = mdata[0]
        try:
            last_updated_time = self.str2time(item['last_updated'])
        except (ValueError, TypeError):
            last_updated_time = now_utc

        age = now_utc - last_updated_time
        self.load_minutes_age = age.days
        self.load_minutes = self.minute_data(mdata, self.max_days_previous, now_utc, 'consumption', 'last_updated', backwards=True, smoothing=True, scale=self.load_scaling, clean_increment=True)
        self.import_today = self.minute_data(mdata, self.max_days_previous, now_utc, 'import', 'last_updated', backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True)
        self.export_today = self.minute_data(mdata, self.max_days_previous, now_utc, 'export', 'last_updated', backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True)
        self.pv_today = self.minute_data(mdata, self.max_days_previous, now_utc, 'pv', 'last_updated', backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True)

        self.load_minutes_now = self.load_minutes.get(0, 0) - self.load_minutes.get(self.minutes_now, 0)
        self.import_today_now = self.import_today.get(0, 0) - self.import_today.get(self.minutes_now, 0)
        self.export_today_now = self.export_today.get(0, 0) - self.export_today.get(self.minutes_now, 0)
        self.pv_today_now = self.pv_today.get(0, 0) - self.pv_today.get(self.minutes_now, 0)
        self.log("Downloaded {} datapoints from GE going back {} days".format(len(self.load_minutes), self.load_minutes_age))
        return True

    def download_predbat_releases_url(self, url):
        """
        Download release data from github, but use the cache for 2 hours
        """
        releases = []

        # Check the cache first
        now = datetime.now()
        if url in self.github_url_cache:
            stamp = self.github_url_cache[url]['stamp']
            pdata = self.github_url_cache[url]['data']
            age = now - stamp
            if age.seconds < (120 * 60):
                self.log("Using cached GITHub data for {} age {} minutes".format(url, age.seconds / 60))
                return pdata

        try:
            r = requests.get(url)
        except:
            self.log("WARN: Unable to load data from Github url: {}".format(url))
            return []

        try:
            pdata = r.json()
        except requests.exceptions.JSONDecodeError:
            self.log("WARN: Unable to decode data from Github url: {}".format(url))
            return []
        
        # Save to cache
        self.github_url_cache[url] = {}
        self.github_url_cache[url]['stamp'] = now
        self.github_url_cache[url]['data'] = pdata

        return pdata
    
    def download_predbat_releases(self):
        """
        Download release data
        """
        url = "https://api.github.com/repos/springfall2008/batpred/releases"
        data = self.download_predbat_releases_url(url)
        self.releases = {}
        if data and isinstance(data, list):
            found_latest = False

            release = data[0]
            self.releases['this'] = THIS_VERSION
            self.releases['latest'] = 'Unknown'

            for release in data:
                if release.get('tag_name', 'Unknown') == THIS_VERSION:
                    self.releases['this_name'] = release.get('name', 'Unknown')
                    self.releases['this_body'] = release.get('body', 'Unknown')

                if not found_latest and not release.get('prerelease', True):
                    self.releases['latest'] = release.get('tag_name', 'Unknown')
                    self.releases['latest_name'] = release.get('name', 'Unknown')
                    self.releases['latest_body'] = release.get('body', 'Unknown')
                    found_latest = True

            self.log("Predbat version {} currently running, latest version is {}".format(self.releases['this'], self.releases['latest']))
        else:
            self.log("WARN: Unable to download Predbat version information from github, return code: {}".format(data))

        return self.releases

    def download_octopus_rates(self, url):
        """
        Download octopus rates directly from a URL or return from cache if recent
        Retry 3 times and then throw error
        """

        # Check the cache first
        now = datetime.now()
        if url in self.octopus_url_cache:
            stamp = self.octopus_url_cache[url]['stamp']
            pdata = self.octopus_url_cache[url]['data']
            age = now - stamp
            if age.seconds < (30 * 60):
                self.log("Return cached octopus data for {} age {} minutes".format(url, age.seconds / 60))
                return pdata

        # Retry up to 3 minutes
        for retry in range(0, 3):
            pdata = self.download_octopus_rates_func(url)
            if pdata:
                break

        # Download failed?
        if not pdata:
            self.log("WARN: Unable to download Octopus data from URL {}".format(url))
            self.record_status("Warn - Unable to download Octopus data from cloud", debug=url, had_errors=True)
            if url in self.octopus_url_cache:
                pdata = self.octopus_url_cache[url]['data']
                return pdata
            else:
                raise ValueError
        
        # Cache New Octopus data
        self.octopus_url_cache[url] = {}
        self.octopus_url_cache[url]['stamp'] = now
        self.octopus_url_cache[url]['data'] = pdata
        return pdata

    def download_octopus_rates_func(self, url):
        """
        Download octopus rates directly from a URL
        """
        mdata = []

        pages = 0

        while url and pages < 3:
            if self.debug_enable:
                self.log("Download {}".format(url))
            r = requests.get(url)
            try:
                data = r.json()       
            except requests.exceptions.JSONDecodeError:
                self.log("WARN: Error downloading Octopus data from url {}".format(url))
                self.record_status("Warn - Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            if 'results' in data:
                mdata += data['results']
            else:
                self.log("WARN: Error downloading Octopus data from url {}".format(url))
                self.record_status("Warn - Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            url = data.get('next', None)
            pages += 1
        pdata = self.minute_data(mdata, self.forecast_days + 1, self.midnight_utc, 'value_inc_vat', 'valid_from', backwards=False, to_key='valid_to')
        return pdata

    def minutes_to_time(self, updated, now):
        """
        Compute the number of minutes between a time (now) and the updated time
        """
        timeday = updated - now
        minutes = int(timeday.seconds / 60) + int(timeday.days * 60*24)
        return minutes

    def str2time(self, str):
        if '.' in str:
            tdata = datetime.strptime(str, TIME_FORMAT_SECONDS)
        else:
            tdata = datetime.strptime(str, TIME_FORMAT)
        return tdata

    def minute_data_import_export(self, now_utc, key):
        """
        Download one or more entities for import/export data
        """
        entity_ids = self.get_arg(key, indirect=False)
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        import_today = {}    
        for entity_id in entity_ids:
            try:
                history = self.get_history(entity_id = entity_id, days = self.max_days_previous)
            except (ValueError, TypeError):
                history = []

            if history:
                import_today = self.minute_data(history[0], self.max_days_previous, now_utc, 'state', 'last_updated', backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True, accumulate=import_today)
            else:
                self.log("WARN: Unable to fetch history for {}".format(entity_id))
                self.record_status("Warn - Unable to fetch history from {}".format(entity_id), had_errors=True)

        return import_today

    def minute_data_load(self, now_utc, entity_name, max_days_previous):
        """
        Download one or more entities for load data
        """
        entity_ids = self.get_arg(entity_name, indirect=False)
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        load_minutes = {}
        age_days = None
        for entity_id in entity_ids:
            history = self.get_history(entity_id = entity_id, days = max_days_previous)
            if history:
                item = history[0][0]
                try:
                    last_updated_time = self.str2time(item['last_updated'])
                except (ValueError, TypeError):
                    last_updated_time = now_utc
                age = now_utc - last_updated_time
                if age_days is None:
                    age_days = age.days
                else:
                    age_days = min(age_days, age.days)
                load_minutes = self.minute_data(history[0], max_days_previous, now_utc, 'state', 'last_updated', backwards=True, smoothing=True, scale=self.load_scaling, clean_increment=True, accumulate=load_minutes)
            else:
                self.log("WARN: Unable to fetch history for {}".format(entity_id))
                self.record_status("Warn - Unable to fetch history from {}".format(entity_id), had_errors=True)
        if age_days is None:
            age_days = 0
        return load_minutes, age_days

    def minute_data(self, history, days, now, state_key, last_updated_key,
                    backwards=False, to_key=None, smoothing=False, clean_increment=False, divide_by=0, scale=1.0, accumulate=[], adjust_key=None):
        """
        Turns data from HA into a hash of data indexed by minute with the data being the value
        Can be backwards in time for history (N minutes ago) or forward in time (N minutes in the future)
        """
        mdata = {}
        adata = {}
        newest_state = 0
        last_state = 0
        newest_age = 999999
        prev_last_updated_time = None

        # Check history is valid
        if not history:
            self.log("Warning, empty history passed to minute_data, ignoring (check your settings)...")
            return mdata

        # Process history
        for item in history:

            # Ignore data without correct keys
            if state_key not in item:
                continue
            if last_updated_key not in item:
                continue

            # Unavailable or bad values
            if item[state_key] == 'unavailable' or item[state_key] == 'unknown':
                continue

            # Get the numerical key and the timestamp and ignore if in error
            try:
                state = float(item[state_key]) * scale
                last_updated_time = self.str2time(item[last_updated_key])
            except (ValueError, TypeError):
                continue

            # Divide down the state if required
            if divide_by:
                state /= divide_by
            
            # Update prev to the first if not set
            if not prev_last_updated_time:
                prev_last_updated_time = last_updated_time
                last_state = state

            # Intelligent adjusted?
            if adjust_key:
                adjusted = item.get(adjust_key, False)
            else:
                adjusted = False

            # Work out end of time period
            # If we don't get it assume it's to the previous update, this is for historical data only (backwards)
            if to_key:
                to_value = item[to_key]
                if not to_value:
                    to_time = now + timedelta(minutes=24*60*self.forecast_days)
                else:
                    to_time = self.str2time(item[to_key])
            else:
                if backwards:
                    to_time = prev_last_updated_time
                else:
                    to_time = None

            if backwards:
                timed = now - last_updated_time
                if to_time:
                    timed_to = now - to_time
            else:
                timed = last_updated_time - now
                if to_time:
                    timed_to = to_time - now

            minutes = int(timed.seconds / 60) + int(timed.days * 60*24)
            if to_time:
                minutes_to = int(timed_to.seconds / 60) + int(timed_to.days * 60*24)

            if minutes < newest_age:
                newest_age = minutes
                newest_state = state

            if to_time:
                minute = minutes
                if minute == minutes_to:
                    mdata[minute] = state
                else:
                    if smoothing:
                        # Reset to zero, sometimes not exactly zero
                        if state < last_state and (state <= (last_state / 10.0)):
                            while minute < minutes_to:
                                mdata[minute] = state
                                minute += 1
                        else:
                            # Can't really go backwards as incrementing data
                            if state < last_state:
                                state = last_state
                            # Create linear function
                            diff = (state - last_state) / (minutes_to - minute)
                            index = 0
                            while minute < minutes_to:
                                mdata[minute] = state - diff*index
                                minute += 1
                                index += 1
                    else:
                        while minute < minutes_to:
                            mdata[minute] = state
                            if adjusted:
                                adata[minute] = True
                            minute += 1
            else:
                mdata[minutes] = state

            # Store previous time & state
            prev_last_updated_time = last_updated_time
            last_state = state

        # If we only have a start time then fill the gaps with the last values
        if not to_key:
            state = newest_state
            for minute in range(0, 60*24*days):
                rindex = 60*24*days - minute - 1
                state = mdata.get(rindex, state)
                mdata[rindex] = state
                minute += 1

        # Reverse data with smoothing 
        if clean_increment:
            mdata = self.clean_incrementing_reverse(mdata)

        # Accumulate to previous data?
        if accumulate:
            for minute in range(0, 60*24*days):
                if minute in mdata:
                    mdata[minute] += accumulate.get(minute, 0)
                else:
                    mdata[minute] = accumulate.get(minute, 0)

        if adjust_key:
            self.io_adjusted = adata
        return mdata

    def minutes_since_yesterday(self, now):
        """
        Calculate the number of minutes since 23:59 yesterday
        """
        yesterday = now - timedelta(days=1)
        yesterday_at_2359 = datetime.combine(yesterday, datetime.max.time())
        difference = now - yesterday_at_2359
        difference_minutes = int((difference.seconds + 59) / 60)
        return difference_minutes

    def dp2(self, value):
        """
        Round to 2 decimal places
        """
        return round(value*100)/100

    def dp3(self, value):
        """
        Round to 3 decimal places
        """
        return round(value*1000)/1000

    def in_charge_window(self, charge_window, minute_abs):
        """
        Work out if this minute is within the a charge window
        """
        window_n = 0
        for window in charge_window:
            if minute_abs >= window['start'] and minute_abs < window['end']:
                return window_n
            window_n += 1
        return -1

    def clean_incrementing_reverse(self, data):
        """
        Cleanup an incrementing sensor data that runs backwards in time to remove the
        resets (where it goes back to 0) and make it always increment
        """
        new_data = {}
        length = max(data) + 1

        increment = 0
        last = data[length - 1]

        for index in range(0, length):
            rindex = length - index - 1
            nxt = data.get(rindex, last)
            if nxt >= last:
                increment += nxt - last
            last = nxt
            new_data[rindex] = increment

        return new_data

    def previous_days_modal_filter(self, data):
        """
        Look at the data from previous days and discard the best case one
        """

        total_points = len(self.days_previous)
        sum_days = []
        min_sum = 99999999
        min_sum_day = 0

        idx = 0
        for days in self.days_previous:
            use_days = min(days, self.load_minutes_age)
            sum_day = 0
            if use_days > 0:
                full_days = 24*60*(use_days - 1)
                for minute in range(0, 24*60):
                    minute_previous = 24 * 60 - minute + full_days
                    load_yesterday = self.get_from_incrementing(data, minute_previous)
                    # Car charging hold
                    if self.car_charging_hold and self.car_charging_energy:
                        # Hold based on data
                        car_energy = self.get_from_incrementing(self.car_charging_energy, minute_previous)
                        load_yesterday = max(0, load_yesterday - car_energy)
                    elif self.car_charging_hold and (load_yesterday >= (self.car_charging_threshold)):
                        # Car charging hold - ignore car charging in computation based on threshold
                        load_yesterday = max(load_yesterday - (self.car_charging_rate[0] / 60.0), 0)
                    sum_day += load_yesterday
            sum_days.append(self.dp2(sum_day))
            if sum_day < min_sum:
                min_sum_day = days
                min_sum_day_idx = idx
                min_sum = self.dp2(sum_day)
            idx += 1
        
        self.log("Historical data totals for days {} are {} - min {}".format(self.days_previous, sum_days, min_sum))
        if self.load_filter_modal and total_points >= 2 and (min_sum_day > 0):
            self.log("Model filter enabled - Discarding day {} as it is the lowest of the {} datapoints".format(min_sum_day, len(self.days_previous)))
            del self.days_previous[min_sum_day_idx]
            del self.days_previous_weight[min_sum_day_idx]

    def get_historical(self, data, minute):
        """
        Get historical data across N previous days in days_previous array based on current minute 
        """
        total = 0
        total_weight = 0
        this_point = 0

        # No data?
        if not data:
            return 0

        for days in self.days_previous:
            use_days = min(days, self.load_minutes_age)
            weight = self.days_previous_weight[this_point]                
            if use_days > 0:
                full_days = 24*60*(use_days - 1)
                minute_previous = 24 * 60 - minute + full_days
                value = self.get_from_incrementing(data, minute_previous)
                total += value * weight
                total_weight += weight
            this_point += 1
    
        # Zero data?
        if total_weight == 0:
            return 0
        else:
            return total / total_weight

    def get_from_incrementing(self, data, index):
        """
        Get a single value from an incrementing series e.g. kwh today -> kwh this minute
        """
        while index < 0:
            index += 24*60
        return data.get(index, 0) - data.get(index + 1, 0)

    def record_length(self, charge_window):
        """
        Limit the forecast length to either the total forecast duration or the start of the last window that falls outside the forecast
        """
        next_charge_start = self.forecast_minutes
        if charge_window:
            next_charge_start = charge_window[0]['start']

        end_record = min(self.forecast_plan_hours * 60 + next_charge_start, self.forecast_minutes + self.minutes_now)
        max_windows = self.max_charge_windows(end_record, charge_window)
        if len(charge_window) > max_windows:
            end_record = min(end_record, charge_window[max_windows]['start'])
            # If we are within this window then push to the end of it
            if end_record < self.minutes_now:
                end_record = charge_window[max_windows]['end']
        return end_record - self.minutes_now
    
    def max_charge_windows(self, end_record_abs, charge_window):
        """
        Work out how many charge windows the time period covers
        """
        charge_windows = 0
        window_n = 0
        for window in charge_window:
            if end_record_abs >= window['end']:
                charge_windows = window_n + 1
            window_n += 1
        return charge_windows

    def record_status(self, message, debug="", had_errors = False):
        """
        Records status to HA sensor
        """
        self.set_state(self.prefix + ".status", state=message, attributes = {'friendly_name' : 'Status', 'icon' : 'mdi:information', 'last_updated' : datetime.now(), 'debug' : debug})
        if had_errors:
            self.had_errors = True

    def scenario_summary_title(self, record_time):
        txt = ""
        for minute in range(0, self.forecast_minutes, 60):
            minute_absolute = minute + self.minutes_now
            minute_timestamp = self.midnight_utc + timedelta(seconds=60*minute_absolute)
            dstamp = minute_timestamp.strftime(TIME_FORMAT)
            stamp = minute_timestamp.strftime("%H:%M")
            if record_time[dstamp] > 0:
                break
            if txt:
                txt += ', '
            txt += "%7s" % str(stamp)
        return txt

    def scenario_summary(self, record_time, datap):
        txt = ""
        for minute in range(0, self.forecast_minutes, 60):
            minute_absolute = minute + self.minutes_now
            minute_timestamp = self.midnight_utc + timedelta(seconds=60*minute_absolute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            value = datap[stamp]
            if not isinstance(value, str):
                value = self.dp2(value)
            if record_time[stamp] > 0:
                break
            if txt:
                txt += ', '
            txt += "%7s" % str(value)
        return txt

    def step_data_history(self, item, minutes_now, forward, step=PREDICT_STEP):
        """
        Create cached step data for historical array 
        """
        minute = 0
        values = {}
        while minute < self.forecast_minutes:
            value = 0
            for offset in range(0, step):
                if forward:
                    value += item.get(minute + minutes_now + offset, 0.0)
                else:
                    value += self.get_historical(item, minute - offset)
            values[minute] = value
            minute += 1
        return values

    def calc_percent_limit(self, charge_limit):
        """
        Calculate a charge limit in percent
        """
        return [min(int((float(charge_limit[i]) / self.soc_max * 100.0) + 0.5), 100) for i in range(0, len(charge_limit))]

    def run_prediction(self, charge_limit, charge_window, discharge_window, discharge_limits, load_minutes_step, pv_forecast_minute_step, save=None, step=PREDICT_STEP, end_record=None):
        """
        Run a prediction scenario given a charge limit, options to save the results or not to HA entity
        """
        predict_soc = {}
        predict_export = {}
        predict_battery_power = {}
        predict_battery_cycle = {}
        predict_soc_time = {}
        predict_car_soc_time = [{} for car_n in range(0, self.num_cars)]
        predict_pv_power = {}
        predict_state = {}
        predict_grid_power = {}
        predict_load_power = {}
        predict_iboost = {}
        minute = 0
        minute_left = self.forecast_minutes
        soc = self.soc_kw
        soc_min = self.soc_max
        soc_min_minute = self.minutes_now
        charge_has_run = False
        charge_has_started = False
        discharge_has_run = False
        export_kwh = self.export_today_now
        export_kwh_h0 = export_kwh
        import_kwh = self.import_today_now
        import_kwh_h0 = import_kwh
        load_kwh = self.load_minutes_now
        load_kwh_h0 = load_kwh
        pv_kwh = self.pv_today_now
        pv_kwh_h0 = pv_kwh
        iboost_today_kwh = self.iboost_today
        import_kwh_house = 0
        import_kwh_battery = 0
        battery_cycle = 0
        final_export_kwh = export_kwh
        final_import_kwh = import_kwh
        final_load_kwh = load_kwh
        final_pv_kwh = pv_kwh
        final_iboost_kwh = iboost_today_kwh
        final_import_kwh_house = import_kwh_house
        final_import_kwh_battery = import_kwh_battery
        final_battery_cycle = battery_cycle
        metric = self.cost_today_sofar
        final_soc = soc
        final_metric = metric
        metric_time = {}
        load_kwh_time = {}
        pv_kwh_time = {}
        export_kwh_time = {}
        import_kwh_time = {}
        record_time = {}
        car_soc = self.car_charging_soc[:]
        final_car_soc = car_soc[:]
        charge_rate_max = self.charge_rate_max
        discharge_rate_max = self.discharge_rate_max
        battery_state = "-"
        grid_state = '-'
        first_charge = end_record
        export_to_first_charge = 0

        # self.log("Sim discharge window {} enable {}".format(discharge_window, discharge_limits))
        charge_limit, charge_window = self.remove_intersecting_windows(charge_limit, charge_window, discharge_limits, discharge_window)

        # For the SOC calculation we need to stop 24 hours after the first charging window starts
        # to avoid wrapping into the next day
        if not end_record:
            end_record = self.record_length(charge_window)
        record = True

        # Simulate each forward minute
        while minute < self.forecast_minutes:
            # Minute yesterday can wrap if days_previous is only 1 
            minute_absolute = minute + self.minutes_now
            minute_timestamp = self.midnight_utc + timedelta(seconds=60*minute_absolute)
            charge_window_n = self.in_charge_window(charge_window, minute_absolute)
            discharge_window_n = self.in_charge_window(discharge_window, minute_absolute)

            # Add in standing charge
            if (minute_absolute % (24*60)) < step:
                metric += self.metric_standing_charge

            # Outside the recording window?
            if minute >= end_record and record:
                record = False

            # Store data before the next simulation step to align timestamps
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if (minute % 10) == 0:
                predict_soc_time[stamp] = self.dp3(soc)
                metric_time[stamp] = self.dp2(metric)
                load_kwh_time[stamp] = self.dp3(load_kwh)
                pv_kwh_time[stamp] = self.dp2(pv_kwh)
                import_kwh_time[stamp] = self.dp2(import_kwh)
                export_kwh_time[stamp] = self.dp2(export_kwh)
                for car_n in range(0, self.num_cars):
                    predict_car_soc_time[car_n][stamp] = self.dp2(car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0)
                record_time[stamp] = 0 if record else self.soc_max
                predict_iboost[stamp] = iboost_today_kwh

            # Save Soc prediction data as minutes for later use
            self.predict_soc[minute] = self.dp3(soc)
            if save and save=='best':
                self.predict_soc_best[minute] = self.dp3(soc)

            # Get load and pv forecast, total up for all values in the step
            pv_now = pv_forecast_minute_step[minute]
            load_yesterday = load_minutes_step[minute]

            # Count PV kwh
            pv_kwh += pv_now
            if record:
                final_pv_kwh = pv_kwh

            # Car charging hold
            if self.car_charging_hold and self.car_charging_energy_step:
                # Hold based on data
                car_energy = self.car_charging_energy_step[minute]
                load_yesterday = max(0, load_yesterday - car_energy)
            elif self.car_charging_hold and (load_yesterday >= (self.car_charging_threshold * step)):
                # Car charging hold - ignore car charging in computation based on threshold
                load_yesterday = max(load_yesterday - (self.car_charging_rate[0] * step / 60.0), 0)

            # Simulate car charging
            car_load = self.in_car_slot(minute_absolute)

            # Car charging?
            car_freeze = False
            for car_n in range(0, self.num_cars):
                if car_load[car_n] > 0.0:
                    car_load_scale = car_load[car_n] * step / 60.0
                    car_load_scale = car_load_scale * self.car_charging_loss
                    car_load_scale = max(min(car_load_scale, self.car_charging_limit[car_n] - car_soc[car_n]), 0)
                    car_soc[car_n] += car_load_scale
                    load_yesterday += car_load_scale / self.car_charging_loss
                    # Model not allowing the car to charge from the battery
                    if not self.car_charging_from_battery:
                        discharge_rate_max = 0
                        car_freeze = True

            # Reset modelled discharge rate if no car is charging
            if not self.car_charging_from_battery and not car_freeze:
                discharge_rate_max = self.battery_rate_max_discharge_scaled

            # Count load
            load_kwh += load_yesterday
            if record:
                final_load_kwh = load_kwh

            # Work out how much PV is used to satisfy home demand
            pv_ac = min(load_yesterday / self.inverter_loss, pv_now, self.inverter_limit * step)

            # And hence how much maybe left for DC charging
            pv_dc = pv_now - pv_ac

            # Scale down PV AC and DC for inverter loss (if hybrid we will reverse the DC loss later)
            pv_ac *= self.inverter_loss
            pv_dc *= self.inverter_loss

            # IBoost model
            if self.iboost_enable:
                iboost_amount = 0
                if iboost_today_kwh < self.iboost_max_energy:
                    if pv_dc > (self.iboost_min_power * step) and ((soc * 100.0 / self.soc_max) >= self.iboost_min_soc):
                        iboost_amount = min(pv_dc, self.iboost_max_power * step)
                        pv_dc -= iboost_amount

                # Cumulative energy
                iboost_today_kwh += iboost_amount   

                # Model Iboost reset
                if (minute_absolute % (24*60)) >= (23*60 + 30):
                    iboost_today_kwh = 0

                # Save Iboost next prediction
                if minute == 0 and save=='best':
                    scaled_boost = (iboost_amount / step) * self.get_arg('run_every', 5)
                    self.iboost_next = self.dp3(self.iboost_today + scaled_boost)
                    self.log("IBoost model predicts usage {} in this run period taking total to {}".format(self.dp2(scaled_boost), self.iboost_next))

            # discharge freeze?
            if self.set_discharge_freeze:
                charge_rate_max = self.battery_rate_max_charge_scaled
                if (discharge_window_n >= 0) and discharge_limits[discharge_window_n] < 100.0:
                    # Freeze mode
                    charge_rate_max = 0

            # Battery behaviour
            battery_draw = 0
            soc_percent = int(soc * 100.0 / self.soc_max + 0.5)
            charge_rate_max_curve = charge_rate_max * self.battery_charge_power_curve.get(soc_percent, 1.0)
            if not self.set_discharge_freeze_only and (discharge_window_n >= 0) and discharge_limits[discharge_window_n] < 100.0 and soc > ((self.soc_max * discharge_limits[discharge_window_n]) / 100.0):
                # Discharge enable
                discharge_rate_max = self.battery_rate_max_discharge_scaled  # Assume discharge becomes enabled here

                # It's assumed if SOC hits the expected reserve then it's terminated
                reserve_expected = (self.soc_max * discharge_limits[discharge_window_n]) / 100.0
                battery_draw = discharge_rate_max * step
                if (soc - reserve_expected) < battery_draw:
                    battery_draw = max(soc - reserve_expected, 0)
                
                # Account for export limit, clip battery draw if possible to avoid going over
                diff_tmp = load_yesterday - (battery_draw + pv_dc + pv_ac)
                if diff_tmp < 0:
                    if abs(diff_tmp) > (self.export_limit * step):
                        above_limit = abs(diff_tmp + self.export_limit * step)
                        battery_draw = max(0, battery_draw - above_limit)

                # Account for inverter limit, clip battery draw if possible to avoid going over
                total_inverted = pv_ac + pv_dc + battery_draw
                if total_inverted > self.inverter_limit * step:
                    reduce_by = total_inverted - (self.inverter_limit * step)
                    battery_draw = max(0, battery_draw - reduce_by)

                battery_state = 'f-'
            elif (charge_window_n >= 0) and soc < charge_limit[charge_window_n]:
                # Charge enable
                charge_rate_max = self.battery_rate_max_charge_scaled  # Assume charge becomes enabled here
                charge_rate_max_curve = charge_rate_max * self.battery_charge_power_curve.get(soc_percent, 1.0)
                battery_draw = -max(min(charge_rate_max_curve * step, charge_limit[charge_window_n] - soc), 0)
                battery_state = 'f+'
                first_charge = min(first_charge, minute)
            else:
                # ECO Mode
                if load_yesterday - pv_ac - pv_dc > 0:
                    battery_draw = min(load_yesterday - pv_ac - pv_dc, discharge_rate_max * step, self.inverter_limit * step - pv_ac)
                    battery_state = 'e-'
                else:
                    battery_draw = max(load_yesterday - pv_ac - pv_dc, -charge_rate_max_curve * step)
                    if battery_draw < 0:
                        battery_state = 'e+'
                    else:
                        battery_state = 'e~'

            # Clamp battery at reserve for discharge 
            if battery_draw > 0:
                # All battery discharge must go through the inverter too
                soc -= battery_draw / (self.battery_loss_discharge * self.inverter_loss)
                if soc < self.reserve:
                    battery_draw -= (self.reserve - soc) * self.battery_loss_discharge * self.inverter_loss
                    soc = self.reserve

            # Clamp battery at max when charging
            if battery_draw < 0:
                battery_draw_dc = max(-pv_dc, battery_draw)
                battery_draw_ac = battery_draw - battery_draw_dc

                if self.inverter_hybrid:
                    inverter_loss = self.inverter_loss
                else:
                    inverter_loss = 1.0

                # In the hybrid case only we remove the inverter loss for PV charging (as it's DC to DC), and inverter loss was already applied
                soc -= battery_draw_dc * self.battery_loss / inverter_loss
                if soc > self.soc_max:
                    battery_draw_dc += ((soc - self.soc_max) / self.battery_loss) * inverter_loss
                    soc = self.soc_max

                # The rest of this charging must be from the grid (pv_dc was the left over PV)
                soc -= battery_draw_ac * self.battery_loss * self.inverter_loss
                if soc > self.soc_max:
                    battery_draw_ac += (soc - self.soc_max) / (self.battery_loss * self.inverter_loss)
                    soc = self.soc_max
                
                #if (minute % 30) == 0:
                #    self.log("Minute {} pv_ac {} pv_dc {} battery_ac {} battery_dc {} battery b4 {} after {} soc {}".format(minute, pv_ac, pv_dc, battery_draw_ac, battery_draw_dc, battery_draw, battery_draw_ac + battery_draw_dc, soc))

                battery_draw = battery_draw_ac + battery_draw_dc

            # Count battery cycles
            battery_cycle += abs(battery_draw)

            # Work out left over energy after battery adjustment
            diff = load_yesterday - (battery_draw + pv_dc + pv_ac)
            if diff < 0:
                # Can not export over inverter limit, load must be taken out first from the inverter limit
                # All exports must come from PV or from the battery, so inverter loss is already accounted for in both cases
                inverter_left = self.inverter_limit * step - load_yesterday
                if inverter_left < 0:
                    diff += -inverter_left
                else:
                    diff = max(diff, -inverter_left)
            if diff < 0:
                # Can not export over export limit, so cap at that
                diff = max(diff, -self.export_limit * step)

            if diff > 0:
                # Import
                # All imports must go to home (no inverter loss) or to the battery (inverter loss accounted before above)
                import_kwh += diff
                if charge_window_n >= 0:
                    # If the battery is on charge anyhow then imports are at battery charging rate
                    import_kwh_battery += diff
                else:
                    # self.log("importing to minute %s amount %s kw total %s kwh total draw %s" % (minute, energy, import_kwh_house, diff))
                    import_kwh_house += diff

                if minute_absolute in self.rate_import:
                    metric += self.rate_import[minute_absolute] * diff
                grid_state = '<'
            else:
                # Export
                energy = -diff
                export_kwh += energy
                if minute_absolute in self.rate_export:
                    metric -= self.rate_export[minute_absolute] * energy
                if diff != 0:
                    grid_state = '>'
                else:
                    grid_state = '~'
            
            # Store the number of minutes until the battery runs out
            if record and soc <= self.reserve:
                minute_left = min(minute, minute_left)

            # Record final soc & metric
            if record:
                final_soc = soc
                for car_n in range(0, self.num_cars):
                    final_car_soc[car_n] = car_soc[car_n]

                final_metric = metric
                final_import_kwh = import_kwh
                final_import_kwh_battery = import_kwh_battery
                final_import_kwh_house = import_kwh_house
                final_export_kwh = export_kwh
                final_iboost_kwh = iboost_today_kwh
                final_battery_cycle = battery_cycle

                # Store export data
                if diff < 0:
                    predict_export[minute] = energy
                    if minute <= first_charge:
                        export_to_first_charge += energy
                else:
                    predict_export[minute] = 0

            # Have we past the charging or discharging time?
            if charge_window_n >= 0:
                charge_has_started = True
            if charge_has_started and (charge_window_n < 0):
                charge_has_run = True
            if (discharge_window_n >= 0) and discharge_limits[discharge_window_n] < 100.0:
                discharge_has_run = True

            # Record soc min
            if record and (discharge_has_run or charge_has_run or not charge_window):
                if soc < soc_min:
                    soc_min_minute = minute_absolute
                soc_min = min(soc_min, soc)

            # Record state
            if (minute % 10) == 0:
                predict_state[stamp] = 'g' + grid_state + 'b' + battery_state
                predict_battery_power[stamp] = self.dp3(battery_draw * (60 / step))
                predict_battery_cycle[stamp] = self.dp3(battery_cycle)
                predict_pv_power[stamp] = self.dp3(pv_now  * (60 / step))
                predict_grid_power[stamp] = self.dp3(diff * (60 / step))
                predict_load_power[stamp] = self.dp3(load_yesterday * (60 / step))

            minute += step

        hours_left = minute_left / 60.0

        if self.debug_enable or save:
            self.log("predict {} end_record {} final soc {} kwh metric {} p min_soc {} @ {} kwh load {} pv {}".format(
                      save, self.time_abs_str(end_record + self.minutes_now), self.dp2(final_soc), self.dp2(final_metric), self.dp2(soc_min), self.time_abs_str(soc_min_minute), self.dp2(final_load_kwh), self.dp2(final_pv_kwh)))
            self.log("         [{}]".format(self.scenario_summary_title(record_time)))
            self.log("    SOC: [{}]".format(self.scenario_summary(record_time, predict_soc_time)))
            self.log("  STATE: [{}]".format(self.scenario_summary(record_time, predict_state)))
            self.log("   LOAD: [{}]".format(self.scenario_summary(record_time, load_kwh_time)))
            self.log("     PV: [{}]".format(self.scenario_summary(record_time, pv_kwh_time)))
            self.log(" IMPORT: [{}]".format(self.scenario_summary(record_time, import_kwh_time)))
            self.log(" EXPORT: [{}]".format(self.scenario_summary(record_time, export_kwh_time)))
            if self.iboost_enable:
                self.log(" IBOOST: [{}]".format(self.scenario_summary(record_time, predict_iboost)))
            for car_n in range(0, self.num_cars):
                self.log("   CAR{}: [{}]".format(car_n, self.scenario_summary(record_time, predict_car_soc_time[car_n])))
            self.log(" METRIC: [{}]".format(self.scenario_summary(record_time, metric_time)))

        # Save data to HA state
        if save and save=='base' and not SIMULATE:
            self.set_state(self.prefix + ".battery_hours_left", state=self.dp2(hours_left), attributes = {'friendly_name' : 'Predicted Battery Hours left', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'icon' : 'mdi:timelapse'})
            postfix = ""
            for car_n in range(0, self.num_cars):                
                if car_n > 0:
                    postfix = "_" + str(car_n)
                self.set_state(self.prefix + ".car_soc" + postfix, state=self.dp2(final_car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0), attributes = {'results' : predict_car_soc_time[car_n], 'friendly_name' : 'Car ' + str(car_n) + ' battery SOC', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".soc_kw_h0", state=self.dp3(self.predict_soc[0]), attributes = {'friendly_name' : 'Current SOC kWh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".soc_kw", state=self.dp3(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Predicted SOC kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".battery_power", state=self.dp3(final_soc), attributes = {'results' : predict_battery_power, 'friendly_name' : 'Predicted Battery Power', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".battery_cycle", state=self.dp3(final_battery_cycle), attributes = {'results' : predict_battery_cycle, 'friendly_name' : 'Predicted Battery Cycle', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".pv_power", state=self.dp3(final_soc), attributes = {'results' : predict_pv_power, 'friendly_name' : 'Predicted PV Power', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".grid_power", state=self.dp3(final_soc), attributes = {'results' : predict_grid_power, 'friendly_name' : 'Predicted Grid Power', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".load_power", state=self.dp3(final_soc), attributes = {'results' : predict_load_power, 'friendly_name' : 'Predicted Load Power', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".soc_min_kwh", state=self.dp3(soc_min), attributes = {'time' : self.time_abs_str(soc_min_minute), 'friendly_name' : 'Predicted minimum SOC best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery-arrow-down-outline'})
            self.set_state(self.prefix + ".export_energy", state=self.dp3(final_export_kwh), attributes = {'results' : export_kwh_time, 'export_until_charge_kwh' : export_to_first_charge, 'friendly_name' : 'Predicted exports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-export'})
            self.set_state(self.prefix + ".export_energy_h0", state=self.dp3(export_kwh_h0), attributes = {'friendly_name' : 'Current export kWh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-export'})
            self.set_state(self.prefix + ".load_energy", state=self.dp3(final_load_kwh), attributes = {'results' : load_kwh_time, 'friendly_name' : 'Predicted load', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:home-lightning-bolt'})
            self.set_state(self.prefix + ".load_energy_h0", state=self.dp3(load_kwh_h0), attributes = {'friendly_name' : 'Current load kWh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:home-lightning-bolt'})
            self.set_state(self.prefix + ".pv_energy", state=self.dp3(final_pv_kwh), attributes = {'results' : pv_kwh_time, 'friendly_name' : 'Predicted PV', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:solar-power'})
            self.set_state(self.prefix + ".pv_energy_h0", state=self.dp3(pv_kwh_h0), attributes = {'friendly_name' : 'Current PV kWh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:solar-power'})
            self.set_state(self.prefix + ".import_energy", state=self.dp3(final_import_kwh), attributes = {'results' : import_kwh_time, 'friendly_name' : 'Predicted imports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state(self.prefix + ".import_energy_h0", state=self.dp3(import_kwh_h0), attributes = {'friendly_name' : 'Current import kWh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state(self.prefix + ".import_energy_battery", state=self.dp3(final_import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state(self.prefix + ".import_energy_house", state=self.dp3(final_import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.log("Battery has {} hours left - now at {}".format(hours_left, self.dp2(self.soc_kw)))
            self.set_state(self.prefix + ".metric", state=self.dp2(final_metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            self.set_state(self.prefix + ".duration", state=self.dp2(end_record/60), attributes = {'friendly_name' : 'Prediction duration', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'icon' : 'mdi:arrow-split-vertical'})

        if save and save=='best' and not SIMULATE:
            self.set_state(self.prefix + ".best_battery_hours_left", state=self.dp2(hours_left), attributes = {'friendly_name' : 'Predicted Battery Hours left best', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'icon' : 'mdi:timelapse'})
            postfix = ""
            for car_n in range(0, self.num_cars):                
                if car_n > 0:
                    postfix = "_" + str(car_n)
                self.set_state(self.prefix + ".car_soc_best" + postfix, state=self.dp2(final_car_soc[car_n] / self.car_charging_battery_size[car_n] * 100.0), attributes = {'results' : predict_car_soc_time[car_n], 'friendly_name' : 'Car ' + str(car_n) + ' battery SOC best', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".soc_kw_best", state=self.dp3(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".battery_power_best", state=self.dp3(final_soc), attributes = {'results' : predict_battery_power, 'friendly_name' : 'Predicted Battery Power Best', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".battery_cycle_best", state=self.dp3(final_battery_cycle), attributes = {'results' : predict_battery_cycle, 'friendly_name' : 'Predicted Battery Cycle Best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".pv_power_best", state=self.dp3(final_soc), attributes = {'results' : predict_pv_power, 'friendly_name' : 'Predicted PV Power Best', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".grid_power_best", state=self.dp3(final_soc), attributes = {'results' : predict_grid_power, 'friendly_name' : 'Predicted Grid Power Best', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".load_power_best", state=self.dp3(final_soc), attributes = {'results' : predict_load_power, 'friendly_name' : 'Predicted Load Power Best', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".soc_kw_best_h1", state=self.dp3(self.predict_soc[60]), attributes = {'friendly_name' : 'Predicted SOC kwh best + 1h', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".soc_kw_best_h8", state=self.dp3(self.predict_soc[60*8]), attributes = {'friendly_name' : 'Predicted SOC kwh best + 8h', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".soc_kw_best_h12", state=self.dp3(self.predict_soc[60*12]), attributes = {'friendly_name' : 'Predicted SOC kwh best + 12h', 'state_class': 'measurement', 'unit _of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".best_soc_min_kwh", state=self.dp3(soc_min), attributes = {'time' : self.time_abs_str(soc_min_minute), 'friendly_name' : 'Predicted minimum SOC best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery-arrow-down-outline'})
            self.set_state(self.prefix + ".best_export_energy", state=self.dp3(final_export_kwh), attributes = {'results' : export_kwh_time, 'export_until_charge_kwh' : export_to_first_charge, 'friendly_name' : 'Predicted exports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-export'})
            self.set_state(self.prefix + ".best_load_energy", state=self.dp3(final_load_kwh), attributes = {'results' : load_kwh_time, 'friendly_name' : 'Predicted load best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:home-lightning-bolt'})
            self.set_state(self.prefix + ".best_pv_energy", state=self.dp3(final_pv_kwh), attributes = {'results' : pv_kwh_time, 'friendly_name' : 'Predicted PV best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:solar-power'})
            self.set_state(self.prefix + ".best_import_energy", state=self.dp3(final_import_kwh), attributes = {'results' : import_kwh_time, 'friendly_name' : 'Predicted imports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state(self.prefix + ".best_import_energy_battery", state=self.dp3(final_import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state(self.prefix + ".best_import_energy_house", state=self.dp3(final_import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state(self.prefix + ".best_metric", state=self.dp2(final_metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted best metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            self.set_state(self.prefix + ".record", state=0.0, attributes = {'results' : record_time, 'friendly_name' : 'Prediction window', 'state_class' : 'measurement'})
            self.set_state(self.prefix + ".iboost_best", state=self.dp2(final_iboost_kwh), attributes = {'results' : predict_iboost, 'friendly_name' : 'Predicted IBoost energy best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:water-boiler'})
            self.find_spare_energy(predict_soc, predict_export, step, first_charge)            

        if save and save=='debug' and not SIMULATE:
            self.set_state(self.prefix + ".pv_power_debug", state=self.dp3(final_soc), attributes = {'results' : predict_pv_power, 'friendly_name' : 'Predicted PV Power Debug', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".grid_power_debug", state=self.dp3(final_soc), attributes = {'results' : predict_grid_power, 'friendly_name' : 'Predicted Grid Power Debug', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".load_power_debug", state=self.dp3(final_soc), attributes = {'results' : predict_load_power, 'friendly_name' : 'Predicted Load Power Debug', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".battery_power_debug", state=self.dp3(final_soc), attributes = {'results' : predict_battery_power, 'friendly_name' : 'Predicted Battery Power Debug', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
 
        if save and save=='best10' and not SIMULATE:
            self.set_state(self.prefix + ".soc_kw_best10", state=self.dp3(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh best 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".best10_pv_energy", state=self.dp3(final_pv_kwh), attributes = {'results' : pv_kwh_time, 'friendly_name' : 'Predicted PV best 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:solar-power'})
            self.set_state(self.prefix + ".best10_metric", state=self.dp2(final_metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted best 10% metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            self.set_state(self.prefix + ".best10_export_energy", state=self.dp3(final_export_kwh), attributes = {'results' : export_kwh_time, 'export_until_charge_kwh': export_to_first_charge, 'friendly_name' : 'Predicted exports best 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-export'})
            self.set_state(self.prefix + ".best10_load_energy", state=self.dp3(final_load_kwh), attributes = {'friendly_name' : 'Predicted load best 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:home-lightning-bolt'})
            self.set_state(self.prefix + ".best10_import_energy", state=self.dp3(final_import_kwh), attributes = {'results' : import_kwh_time, 'friendly_name' : 'Predicted imports best 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})

        if save and save=='base10' and not SIMULATE:
            self.set_state(self.prefix + ".soc_kw_base10", state=self.dp3(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh base 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state(self.prefix + ".base10_pv_energy", state=self.dp3(final_pv_kwh), attributes = {'results' : pv_kwh_time, 'friendly_name' : 'Predicted PV base 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:solar-power'})
            self.set_state(self.prefix + ".base10_metric", state=self.dp2(final_metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted base 10% metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            self.set_state(self.prefix + ".base10_export_energy", state=self.dp3(final_export_kwh), attributes = {'results' : export_kwh_time, 'export_until_charge_kwh': export_to_first_charge, 'friendly_name' : 'Predicted exports base 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-export'})
            self.set_state(self.prefix + ".base10_load_energy", state=self.dp3(final_load_kwh), attributes = {'friendly_name' : 'Predicted load base 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:home-lightning-bolt'})
            self.set_state(self.prefix + ".base10_import_energy", state=self.dp3(final_import_kwh), attributes = {'results' : import_kwh_time, 'friendly_name' : 'Predicted imports base 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})

        return final_metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, final_soc, soc_min_minute, final_battery_cycle

    def time_now_str(self):
        """
        Return time now as human string
        """
        return (self.midnight + timedelta(minutes=self.minutes_now)).strftime("%H:%M:%S")

    def time_abs_str(self, minute):
        """
        Return time absolute as human string
        """
        return (self.midnight + timedelta(minutes=minute)).strftime("%m-%d %H:%M:%S")

    def rate_replicate(self, rates, rate_io={}):
        """
        We don't get enough hours of data for Octopus, so lets assume it repeats until told others
        """
        minute = 0
        rate_last = 0
        # Add 48 extra hours to make sure the whole cycle repeats another day
        while minute < (self.forecast_minutes + 48*60):
            if minute not in rates:
                if (minute >= 24*60) and ((minute - 24*60) in rates):
                    minute_mod = minute - 24*60
                else:
                    minute_mod = minute % (24 * 60)
                if (minute_mod in rate_io) and rate_io[minute_mod]:
                    # Dont replicate Intelligent rates into the next day as it will be different
                    rates[minute] = self.rate_max
                elif minute_mod in rates:
                    rates[minute] = rates[minute_mod]
                else:
                    # Missing rate within 24 hours - fill with dummy last rate
                    rates[minute] = rate_last
            else:
                rate_last = rates[minute]
            minute += 1
        return rates

    def find_charge_window(self, rates, minute, threshold_rate, find_high):
        """
        Find the charging windows based on the low rate threshold (percent below average)
        """
        rate_low_start = -1
        rate_low_end = -1
        rate_low_average = 0
        rate_low_rate = 0
        rate_low_count = 0

        stop_at = self.forecast_minutes + self.minutes_now + 12*60
        # Scan for lower rate start and end
        while minute < stop_at:
            # Don't allow starts beyond the forecast window
            if minute >= (self.forecast_minutes + self.minutes_now) and (rate_low_start < 0):
                break

            if minute in rates:
                rate = rates[minute]
                if ((not find_high) and (rate <= threshold_rate)) or (find_high and (rate >= threshold_rate) and (rate > 0)):
                    if (not self.combine_mixed_rates) and (rate_low_start >= 0) and (self.dp2(rate) != self.dp2(rate_low_rate)):
                        # Refuse mixed rates
                        rate_low_end = minute
                        break
                    if find_high and (not self.combine_discharge_slots) and (rate_low_start >= 0) and ((minute - rate_low_start) >= self.discharge_slot_split):
                        # If combine is disabled, for export slots make them all N minutes so we can select some not all
                        rate_low_end = minute
                        break
                    if (not find_high) and (not self.combine_charge_slots) and (rate_low_start >= 0) and ((minute - rate_low_start) >= self.charge_slot_split):
                        # If combine is disabled, for import slots make them all N minutes so we can select some not all
                        rate_low_end = minute
                        break
                    if find_high and (rate_low_start >= 0) and ((minute - rate_low_start) >= 60*6):
                        # Export slot can never be bigger than 6 hours
                        rate_low_end = minute
                        break
                    if rate_low_start < 0:
                        rate_low_start = minute
                        rate_low_end = stop_at
                        rate_low_count = 1
                        rate_low_average = rate
                        rate_low_rate = rate
                    elif rate_low_end > minute:
                        rate_low_average += rate
                        rate_low_count += 1
                else:
                    if rate_low_start >= 0:
                        rate_low_end = minute
                        break                    
            else:
                if rate_low_start >= 0 and rate_low_end >= minute:
                    rate_low_end = minute
                break
            minute += 1

        if rate_low_count:
            rate_low_average = self.dp2(rate_low_average / rate_low_count)
        return rate_low_start, rate_low_end, rate_low_average

    def basic_rates(self, info, rtype, prev=None):
        """
        Work out the energy rates based on user supplied time periods
        works on a 24-hour period only and then gets replicated later for future days
        """
        rates = {}

        if prev:
            rates = prev.copy()
            self.log("Override {} rate info {}".format(rtype, info))
        else:
            # Set to zero
            self.log("Adding {} rate info {}".format(rtype, info))
            for minute in range(0, 24*60):
                rates[minute] = 0

        max_minute = max(rates) + 1
        midnight = datetime.strptime('00:00:00', "%H:%M:%S")
        for this_rate in info:
            start = datetime.strptime(this_rate.get('start', "00:00:00"), "%H:%M:%S")
            end = datetime.strptime(this_rate.get('end', "00:00:00"), "%H:%M:%S")
            date = None
            if 'date' in this_rate:
                date = datetime.strptime(this_rate['date'], "%Y-%m-%d")
            rate = this_rate.get('rate', 0)

            # Time in minutes
            start_minutes = max(self.minutes_to_time(start, midnight), 0)
            end_minutes   = min(self.minutes_to_time(end, midnight), 24*60-1)

            # Make end > start
            if end_minutes <= start_minutes:
                end_minutes += 24*60

            # Adjust for date if specified
            if date:
                delta_minutes = self.minutes_to_time(date, self.midnight)
                start_minutes += delta_minutes
                end_minutes += delta_minutes

            # Store rates against range
            if end_minutes >= 0 and start_minutes < max_minute:
                for minute in range(start_minutes, end_minutes):
                    if (not date) or (minute >= 0 and minute < max_minute):
                        rates[minute % max_minute] = rate

        return rates

    def plan_car_charging(self, car_n, low_rates):
        """
        Plan when the car will charge, taking into account ready time and pricing
        """
        plan = []
        car_soc = self.car_charging_soc[car_n]
        
        if self.car_charging_plan_smart[car_n]:
            price_sorted = self.sort_window_by_price(low_rates)
            price_sorted.reverse()
        else:
            price_sorted = range(0, len(low_rates))

        ready_time = datetime.strptime(self.car_charging_plan_time[car_n], "%H:%M:%S")
        ready_minutes = ready_time.hour * 60 + ready_time.minute

        # Ready minutes wrap?
        if ready_minutes < self.minutes_now:
            ready_minutes += 24*60

        for window_n in price_sorted:
            window = low_rates[window_n]
            start = max(window['start'], self.minutes_now)
            end = min(window['end'], ready_minutes)
            length = 0
            kwh = 0

            if car_soc >= self.car_charging_limit[car_n]:
                break

            if end <= start:
                continue

            length = end - start
            hours = length / 60
            kwh = self.car_charging_rate[car_n] * hours

            kwh_add = kwh * self.car_charging_loss
            kwh_left = self.car_charging_limit[car_n] - car_soc

            # Clamp length to required amount (shorten the window)
            if kwh_add > kwh_left:
                percent = kwh_left / kwh_add
                length = int((length * percent) / 5 + 2.5) * 5
                end = start + length
                hours = length / 60
                kwh = self.car_charging_rate[car_n] * hours
                kwh_add = kwh * self.car_charging_loss

            # Work out how much to add to the battery, include losses
            kwh_add = max(min(kwh_add, self.car_charging_limit[car_n] - car_soc), 0)
            kwh = kwh_add / self.car_charging_loss

            # Work out charging amounts
            if kwh > 0:
                car_soc += kwh_add
                new_slot = {}
                new_slot['start'] = start
                new_slot['end'] = end
                new_slot['kwh'] = kwh
                new_slot['average'] = window['average']
                new_slot['cost'] = new_slot['average'] * kwh
                plan.append(new_slot)


        # Return sorted back in time order
        plan = self.sort_window_by_time(plan)
        return plan

    def load_octopus_slots(self, octopus_slots):
        """
        Turn octopus slots into charging plan
        """
        new_slots = []

        for slot in octopus_slots:
            if 'start' in slot:
                start = datetime.strptime(slot['start'], TIME_FORMAT)
                end = datetime.strptime(slot['end'], TIME_FORMAT)
            else:
                start = datetime.strptime(slot['startDtUtc'], TIME_FORMAT_OCTOPUS)
                end = datetime.strptime(slot['endDtUtc'], TIME_FORMAT_OCTOPUS)
            source = slot.get('source', '')
            start_minutes = max(self.minutes_to_time(start, self.midnight_utc), 0)
            end_minutes   = min(self.minutes_to_time(end, self.midnight_utc), self.forecast_minutes)
            slot_minutes = end_minutes - start_minutes
            slot_hours = slot_minutes / 60.0

            # The load expected is stored in chargeKwh for the period in use
            if 'charge_in_kwh' in slot:
                kwh = abs(float(slot.get('charge_in_kwh', self.car_charging_rate[0] * slot_hours)))
            else:
                kwh = abs(float(slot.get('chargeKwh', self.car_charging_rate[0]  * slot_hours)))

            if end_minutes > self.minutes_now:
                new_slot = {}
                new_slot['start'] = start_minutes
                new_slot['end'] = end_minutes
                new_slot['kwh'] = kwh
                if source != 'bump-charge':
                    new_slot['average'] = self.rate_min  # Assume price in min 
                else:
                    new_slot['average'] = self.rate_max  # Assume price is max 
                new_slot['cost'] = new_slot['average'] * kwh
                new_slots.append(new_slot)
        return new_slots

    def in_car_slot(self, minute):
        """
        Is the given minute inside a car slot
        """
        load_amount = [0 for car_n in range(0, self.num_cars)]

        for car_n in range(0, self.num_cars):
            if self.car_charging_slots[car_n]:
                for slot in self.car_charging_slots[car_n]:
                    start_minutes = slot['start']
                    end_minutes = slot['end']
                    kwh = slot['kwh']
                    slot_minutes = end_minutes - start_minutes
                    slot_hours = slot_minutes / 60.0

                    # Return the load in that slot
                    if minute >= start_minutes and minute < end_minutes:
                        load_amount[car_n] = abs(kwh / slot_hours)
                        break
        return load_amount

    def rate_scan_export(self, rates, print=True):
        """
        Scan the rates and work out min/max
        """

        self.rate_export_min, self.rate_export_max, self.rate_export_average, self.rate_export_min_minute, self.rate_export_max_minute = self.rate_minmax(rates)
        if print:
            self.log("Export rates min {} max {} average {}".format(self.rate_export_min, self.rate_export_max, self.rate_export_average))
        return rates

    def publish_car_plan(self):
        """
        Publish the car charging plan
        """
        plan = []
        postfix = ""
        for car_n in range(self.num_cars):
            if car_n > 0:
                postfix = "_" + str(car_n)
            if not self.car_charging_slots[car_n]:
                self.set_state("binary_sensor." + self.prefix + "_car_charging_slot" + postfix, state='off', attributes = {'planned' : plan, 'cost' : None, 'kwh' : None, 'friendly_name' : 'Predbat car charging slot' + postfix, 'icon': 'mdi:home-lightning-bolt-outline'})
            else:
                window = self.car_charging_slots[car_n][0]
                if self.minutes_now >= window['start'] and self.minutes_now < window['end']:
                    slot = True
                else:
                    slot = False

                total_kwh = 0
                total_cost = 0
                for window in self.car_charging_slots[car_n]:
                    start = self.time_abs_str(window['start'])
                    end = self.time_abs_str(window['end'])
                    kwh = self.dp2(window['kwh'])
                    average = self.dp2(window['average'])
                    cost = self.dp2(window['cost'])
                    
                    show = {}
                    show['start'] = start
                    show['end'] = end
                    show['kwh'] = kwh
                    show['average'] = average
                    show['cost'] = cost
                    total_cost += cost
                    total_kwh += kwh
                    plan.append(show)

                self.set_state("binary_sensor." + self.prefix + "_car_charging_slot" + postfix, state="on" if slot else 'off', attributes = {'planned' : plan, 'cost' : self.dp2(total_cost), 'kwh' : self.dp2(total_kwh), 'friendly_name' : 'Predbat car charging slot' + postfix, 'icon': 'mdi:home-lightning-bolt-outline'})

    def publish_rates_export(self):
        """
        Publish the export rates
        """
        window_str = ""
        if self.high_export_rates:
            window_n = 0
            for window in self.high_export_rates:
                rate_high_start = window['start']
                rate_high_end = window['end']
                rate_high_average = window['average']

                if window_str:
                    window_str += ", "
                window_str += "{} - {} @ {}".format(self.time_abs_str(rate_high_start), self.time_abs_str(rate_high_end), rate_high_average)

                rate_high_start_date = self.midnight_utc + timedelta(minutes=rate_high_start)
                rate_high_end_date = self.midnight_utc + timedelta(minutes=rate_high_end)

                time_format_time = '%H:%M:%S'

                if window_n == 0 and not SIMULATE:
                    self.set_state(self.prefix + ".high_rate_export_start", state=rate_high_start_date.strftime(time_format_time), attributes = {'date' : rate_high_start_date.strftime(TIME_FORMAT), 'friendly_name' : 'Next high export rate start', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state(self.prefix + ".high_rate_export_end", state=rate_high_end_date.strftime(time_format_time), attributes = {'date' : rate_high_end_date.strftime(TIME_FORMAT), 'friendly_name' : 'Next high export rate end', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state(self.prefix + ".high_rate_export_cost", state=self.dp2(rate_high_average), attributes = {'friendly_name' : 'Next high export rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
                    in_high_rate = self.minutes_now >= rate_high_start and self.minutes_now <= rate_high_end
                    self.set_state("binary_sensor." + self.prefix + "_high_rate_export_slot", state='on' if in_high_rate else 'off', attributes = {'friendly_name' : 'Predbat high rate slot', 'icon': 'mdi:home-lightning-bolt-outline'})
                    high_rate_minutes = (rate_high_end - self.minutes_now) if in_high_rate else (rate_high_end - rate_high_start)
                    self.set_state(self.prefix + ".high_rate_export_duration", state=high_rate_minutes, attributes = {'friendly_name' : 'Next high export rate duration', 'state_class': 'measurement', 'unit_of_measurement': 'minutes', 'icon': 'mdi:table-clock'})
                if window_n == 1 and not SIMULATE:
                    self.set_state(self.prefix + ".high_rate_export_start_2", state=rate_high_start_date.strftime(time_format_time), attributes = {'date' : rate_high_start_date.strftime(TIME_FORMAT), 'friendly_name' : 'Next+1 high export rate start', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state(self.prefix + ".high_rate_export_end_2", state=rate_high_end_date.strftime(time_format_time), attributes = {'date' : rate_high_end_date.strftime(TIME_FORMAT), 'friendly_name' : 'Next+1 high export rate end', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state(self.prefix + ".high_rate_export_cost_2", state=self.dp2(rate_high_average), attributes = {'friendly_name' : 'Next+1 high export rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
                window_n += 1

        if window_str:
            self.log("High export rate windows [{}]".format(window_str))

        # Clear rates that aren't available
        if not self.high_export_rates and not SIMULATE:
            self.log("No high rate period found")
            self.set_state(self.prefix + ".high_rate_export_start", state='undefined', attributes = {'date' : None, 'friendly_name' : 'Next high export rate start', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state(self.prefix + ".high_rate_export_end", state='undefined', attributes = {'date' : None, 'friendly_name' : 'Next high export rate end', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state(self.prefix + ".high_rate_export_cost", state=self.dp2(self.rate_export_average), attributes = {'friendly_name' : 'Next high export rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            self.set_state("binary_sensor." + self.prefix + "_high_rate_export_slot", state='off', attributes = {'friendly_name' : 'Predbat high export rate slot', 'icon': 'mdi:home-lightning-bolt-outline'})
            self.set_state(self.prefix + ".high_rate_export_duration", state=0, attributes = {'friendly_name' : 'Next high export rate duration', 'state_class': 'measurement', 'unit_of_measurement': 'minutes', 'icon': 'mdi:table-clock'})
        if len(self.high_export_rates) < 2 and not SIMULATE:
            self.set_state(self.prefix + ".high_rate_export_start_2", state='undefined', attributes = {'date' : None, 'friendly_name' : 'Next+1 high export rate start', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state(self.prefix + ".high_rate_export_end_2", state='undefined', attributes = {'date' : None, 'friendly_name' : 'Next+1 high export rate end', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state(self.prefix + ".high_rate_export_cost_2", state=self.dp2(self.rate_export_average), attributes = {'friendly_name' : 'Next+1 high export rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})


    def rate_minmax(self, rates):
        """
        Work out min and max rates
        """
        rate_min = 99999
        rate_min_minute = 0
        rate_max_minute = 0
        rate_max = 0
        rate_average = 0
        rate_n = 0

        # Scan rates and find min/max/average
        rate = rates.get(self.minutes_now, 0)
        for minute in range(self.minutes_now, self.forecast_minutes + self.minutes_now):
            if minute in rates:
                rate = rates[minute]
                if rate > rate_max:
                    rate_max = rate
                    rate_max_minute = minute
                if rate < rate_min:
                    rate_min = rate
                    rate_min_minute = minute
                rate_average += rate
                rate_n += 1
            minute += 1
        if rate_n:
            rate_average /= rate_n
        
        return self.dp2(rate_min), self.dp2(rate_max), self.dp2(rate_average), rate_min_minute, rate_max_minute

    def rate_min_forward_calc(self, rates):
        """
        Work out lowest rate from time forwards
        """
        rate_array = []
        rate_min_forward = {}
        rate = self.rate_min

        for minute in range(0, self.forecast_minutes + self.minutes_now + 48*60):
            if minute in rates:
                rate = rates[minute]
            rate_array.append(rate)
            
        # Work out the min rate going forward 
        for minute in range(self.minutes_now, self.forecast_minutes + 24*60 + self.minutes_now):
            rate_min_forward[minute] = min(rate_array[minute:])

        self.log("Rate min forward looking: now {} at end of forecast {}".format(rate_min_forward[self.minutes_now], rate_min_forward[self.forecast_minutes]))

        return rate_min_forward

    def rate_scan_window(self, rates, rate_low_min_window, threshold_rate, find_high):
        """
        Scan for the next high/low rate window
        """
        minute = 0
        found_rates = []

        while len(found_rates) < self.max_windows:
            rate_low_start, rate_low_end, rate_low_average = self.find_charge_window(rates, minute, threshold_rate, find_high)
            window = {}
            window['start'] = rate_low_start
            window['end'] = rate_low_end
            window['average'] = rate_low_average

            if rate_low_start >= 0:
                if rate_low_end > self.minutes_now and (rate_low_end - rate_low_start) >= rate_low_min_window:
                    found_rates.append(window)
                minute = rate_low_end
            else:
                break
        return found_rates

    def set_rate_thresholds(self):
        """
        Set the high and low rate thresholds
        """
        self.rate_threshold = self.dp2(self.rate_average * self.rate_low_threshold)
        if self.rate_low_match_export:
            # When enabled the low rate could be anything up-to the export rate (less battery losses)
            self.rate_threshold = self.dp2(max(self.rate_threshold, self.rate_export_max * self.battery_loss * self.battery_loss_discharge))

        # Compute the export rate threshold
        self.rate_export_threshold = self.dp2(self.rate_export_average * self.rate_high_threshold)

        # Rule out exports if the import rate is already higher unless it's a variable export tariff
        if self.rate_export_max == self.rate_export_min:
            self.rate_export_threshold = max(self.rate_export_threshold, self.dp2(self.rate_min))

        self.log("Rate thresholds (for charge/discharge) are import {} export {}".format(self.rate_threshold, self.rate_export_threshold))

    def rate_add_io_slots(self, rates, octopus_slots):
        """
        # Add in any planned octopus slots
        """
        if octopus_slots:
            # Add in IO slots
            for slot in octopus_slots:
                if 'start' in slot:
                    start = datetime.strptime(slot['start'], TIME_FORMAT)
                    end = datetime.strptime(slot['end'], TIME_FORMAT)
                else:
                    start = datetime.strptime(slot['startDtUtc'], TIME_FORMAT_OCTOPUS)
                    end = datetime.strptime(slot['endDtUtc'], TIME_FORMAT_OCTOPUS)
                source = slot.get('source', '')
                # Ignore bump-charge slots as their cost won't change
                if source != 'bump-charge':
                    start_minutes = max(self.minutes_to_time(start, self.midnight_utc), 0)
                    end_minutes   = max(min(self.minutes_to_time(end, self.midnight_utc), self.forecast_minutes), 0)
                    if end_minutes > start_minutes:
                        self.log("Octopus Intelligent slot at {}-{} assumed price {}".format(self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), self.rate_min))
                        for minute in range(start_minutes, end_minutes):
                            rates[minute] = self.rate_min

        return rates

    def rate_scan(self, rates, print=True):
        """
        Scan the rates and work out min/max
        """
        self.low_rates = []
        
        self.rate_min, self.rate_max, self.rate_average, self.rate_min_minute, self.rate_max_minute = self.rate_minmax(rates)

        if print:
            # Calculate minimum forward rates only once rate replicate has run (when print is True)
            self.rate_min_forward = self.rate_min_forward_calc(self.rate_import)
            self.log("Import rates min {} max {} average {}".format(self.rate_min, self.rate_max, self.rate_average))

        return rates

    def publish_rates_import(self):
        """
        Publish the import rates
        """
        window_str = ""
        # Output rate info
        if self.low_rates:
            window_n = 0
            for window in self.low_rates:
                rate_low_start = window['start']
                rate_low_end = window['end']
                rate_low_average = window['average']

                if window_str:
                    window_str += ", "
                window_str += "{}: {} - {} @ {}".format(window_n, self.time_abs_str(rate_low_start), self.time_abs_str(rate_low_end), rate_low_average)

                rate_low_start_date = self.midnight_utc + timedelta(minutes=rate_low_start)
                rate_low_end_date = self.midnight_utc + timedelta(minutes=rate_low_end)

                time_format_time = '%H:%M:%S'
                if window_n == 0 and not SIMULATE:
                    self.set_state(self.prefix + ".low_rate_start", state=rate_low_start_date.strftime(time_format_time), attributes = {'date' : rate_low_start_date.strftime(TIME_FORMAT), 'friendly_name' : 'Next low rate start', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state(self.prefix + ".low_rate_end", state=rate_low_end_date.strftime(time_format_time), attributes = {'date' : rate_low_end_date.strftime(TIME_FORMAT), 'friendly_name' : 'Next low rate end', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state(self.prefix + ".low_rate_cost", state=rate_low_average, attributes = {'friendly_name' : 'Next low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
                    in_low_rate = self.minutes_now >= rate_low_start and self.minutes_now <= rate_low_end
                    self.set_state("binary_sensor." + self.prefix + "_low_rate_slot", state='on' if in_low_rate else 'off', attributes = {'friendly_name' : 'Predbat low rate slot', 'icon': 'mdi:home-lightning-bolt-outline'})
                    low_rate_minutes = (rate_low_end - self.minutes_now) if in_low_rate else (rate_low_end - rate_low_start)
                    self.set_state(self.prefix + ".low_rate_duration", state=low_rate_minutes, attributes = {'friendly_name' : 'Next low rate duration', 'state_class': 'measurement', 'unit_of_measurement': 'minutes', 'icon': 'mdi:table-clock'})
                if window_n == 1 and not SIMULATE:
                    self.set_state(self.prefix + ".low_rate_start_2", state=rate_low_start_date.strftime(time_format_time), attributes = {'date' : rate_low_start_date.strftime(TIME_FORMAT), 'friendly_name' : 'Next+1 low rate start', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state(self.prefix + ".low_rate_end_2", state=rate_low_end_date.strftime(time_format_time), attributes = {'date' : rate_low_end_date.strftime(TIME_FORMAT), 'friendly_name' : 'Next+1 low rate end', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state(self.prefix + ".low_rate_cost_2", state=rate_low_average, attributes = {'friendly_name' : 'Next+1 low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
                window_n += 1

        self.log("Low import rate windows [{}]".format(window_str))

        # Clear rates that aren't available
        if not self.low_rates and not SIMULATE:
            self.log("No low rate period found")
            self.set_state(self.prefix + ".low_rate_start", state='undefined', attributes = {'date' : None, 'friendly_name' : 'Next low rate start', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state(self.prefix + ".low_rate_end", state='undefined', attributes = {'date' : None, 'friendly_name' : 'Next low rate end', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state(self.prefix + ".low_rate_cost", state=self.rate_average, attributes = {'friendly_name' : 'Next low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            self.set_state(self.prefix + ".low_rate_duration", state=0, attributes = {'friendly_name' : 'Next low rate duration', 'state_class': 'measurement', 'unit_of_measurement': 'minutes', 'icon': 'mdi:table-clock'})
            self.set_state("binary_sensor." + self.prefix + "_low_rate_slot", state='off', attributes = {'friendly_name' : 'Predbat low rate slot', 'icon': 'mdi:home-lightning-bolt-outline'})
        if len(self.low_rates) < 2 and not SIMULATE:
            self.set_state(self.prefix + ".low_rate_start_2", state='undefined', attributes = {'date' : None, 'friendly_name' : 'Next+1 low rate start', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state(self.prefix + ".low_rate_end_2", state='undefined', attributes = {'date' : None, 'friendly_name' : 'Next+1 low rate end', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state(self.prefix + ".low_rate_cost_2", state=self.rate_average, attributes = {'friendly_name' : 'Next+1 low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})

    def publish_rates(self, rates, export):
        """
        Publish the rates for charts
        Create rates/time every 30 minutes
        """
        rates_time = {}
        for minute in range(0, self.forecast_minutes+24*60, 30):
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            rates_time[stamp] = self.dp2(rates[minute])

        if export:
            self.publish_rates_export()
        else:
            self.publish_rates_import()

        if not SIMULATE:
            if export:
                self.set_state(self.prefix + ".rates_export", state=self.dp2(rates[self.minutes_now]), attributes = {'min' : self.dp2(self.rate_export_min), 'max' : self.dp2(self.rate_export_max), 'average' : self.dp2(self.rate_export_average), 'threshold' : self.dp2(self.rate_export_threshold), 'results' : rates_time, 'friendly_name' : 'Export rates', 'state_class' : 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            else:
                self.set_state(self.prefix + ".rates", state=self.dp2(rates[self.minutes_now]), attributes = {'min' : self.dp2(self.rate_min), 'max' : self.dp2(self.rate_max), 'average' : self.dp2(self.rate_average), 'threshold' : self.dp2(self.rate_threshold), 'results' : rates_time, 'friendly_name' : 'Import rates', 'state_class' : 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
        return rates

    def today_cost(self, import_today, export_today):
        """
        Work out energy costs today (approx)
        """
        day_cost = 0
        day_cost_import = 0
        day_cost_export = 0
        day_energy = 0
        day_energy_export = 0
        day_cost_time = {}
        day_cost_time_import = {}
        day_cost_time_export = {}

        for minute in range(0, self.minutes_now):
            # Add in standing charge
            if (minute % (24*60)) == 0:
                day_cost += self.metric_standing_charge
                day_cost_import += self.metric_standing_charge

            minute_back = self.minutes_now - minute - 1
            energy = 0
            energy = self.get_from_incrementing(import_today, minute_back)
            if export_today:
                energy_export = self.get_from_incrementing(export_today, minute_back)
            else:
                energy_export = 0
            day_energy += energy
            day_energy_export += energy_export
            
            if self.rate_import:
                day_cost += self.rate_import[minute] * energy
                day_cost_import += self.rate_import[minute] * energy
                
            if self.rate_export:
                day_cost -= self.rate_export[minute] * energy_export
                day_cost_export -= self.rate_export[minute] * energy_export

            if (minute % 10) == 0:
                minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                day_cost_time[stamp] = self.dp2(day_cost)
                day_cost_time_import[stamp] = self.dp2(day_cost_import)
                day_cost_time_export[stamp] = self.dp2(day_cost_export)

        if not SIMULATE:
            self.set_state(self.prefix + ".cost_today", state=self.dp2(day_cost), attributes = {'results' : day_cost_time, 'friendly_name' : 'Cost so far today', 'state_class' : 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            self.set_state(self.prefix + ".cost_today_import", state=self.dp2(day_cost_import), attributes = {'results' : day_cost_time_import, 'friendly_name' : 'Cost so far today import', 'state_class' : 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            self.set_state(self.prefix + ".cost_today_export", state=self.dp2(day_cost_export), attributes = {'results' : day_cost_time_export, 'friendly_name' : 'Cost so far today export', 'state_class' : 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
        self.log("Todays energy import {} kwh export {} kwh cost {} p import {} p export {} p".format(self.dp2(day_energy), self.dp2(day_energy_export), self.dp2(day_cost), self.dp2(day_cost_import), self.dp2(day_cost_export)))
        return day_cost

    def publish_discharge_limit(self, discharge_window, discharge_limits, best):
        """
        Create entity to chart discharge limit
        """
        discharge_limit_time = {}
        discharge_limit_time_kw = {}

        discharge_limit_soc = self.soc_max
        discharge_limit_percent = 100
        discharge_limit_first = False

        for minute in range(0, self.forecast_minutes, 30):
            window_n = self.in_charge_window(discharge_window, minute + self.minutes_now)
            minute_timestamp = self.midnight_utc + timedelta(minutes=(minute + self.minutes_now))
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if window_n >=0 and (discharge_limits[window_n] < 100.0):
                soc_kw = (discharge_limits[window_n] * self.soc_max) / 100.0
                discharge_limit_time[stamp] = discharge_limits[window_n]
                discharge_limit_time_kw[stamp] = soc_kw
                if not discharge_limit_first:
                    discharge_limit_soc = soc_kw
                    discharge_limit_percent = discharge_limits[window_n]
                    discharge_limit_first = True
            else:
                discharge_limit_time[stamp] = 100
                discharge_limit_time_kw[stamp] = self.soc_max

        if not SIMULATE:
            discharge_start_str = 'undefined'
            discharge_end_str = 'undefined'
            discharge_start_date = None
            discharge_end_date = None

            if discharge_window and (discharge_window[0]['end'] < (24*60 + self.minutes_now)):
                discharge_start_minutes = discharge_window[0]['start']
                discharge_end_minutes = discharge_window[0]['end']

                time_format_time = '%H:%M:%S'
                discharge_startt = self.midnight_utc + timedelta(minutes=discharge_start_minutes)
                discharge_endt = self.midnight_utc + timedelta(minutes=discharge_end_minutes)
                discharge_start_str = discharge_startt.strftime(time_format_time)
                discharge_end_str = discharge_endt.strftime(time_format_time)
                discharge_start_date = discharge_startt.strftime(TIME_FORMAT)
                discharge_end_date = discharge_endt.strftime(TIME_FORMAT)

            if best:
                self.set_state(self.prefix + ".best_discharge_limit_kw", state=self.dp2(discharge_limit_soc), attributes = {'results' : discharge_limit_time_kw, 'friendly_name' : 'Predicted discharge limit kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' :'mdi:battery-charging'})
                self.set_state(self.prefix + ".best_discharge_limit", state=discharge_limit_percent, attributes = {'results' : discharge_limit_time, 'friendly_name' : 'Predicted discharge limit best', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' :'mdi:battery-charging'})
                self.set_state(self.prefix + ".best_discharge_start", state=discharge_start_str, attributes = {'timestamp' : discharge_start_date, 'friendly_name' : 'Predicted discharge start time best', 'state_class': 'measurement', 'state_class': 'timestamp', 'icon': 'mdi:table-clock', 'unit_of_measurement' : None})
                self.set_state(self.prefix + ".best_discharge_end", state=discharge_end_str, attributes = {'timestamp' : discharge_end_date, 'friendly_name' : 'Predicted discharge end time best', 'state_class': 'measurement', 'state_class': 'timestamp', 'icon': 'mdi:table-clock', 'unit_of_measurement' : None})
            else:
                self.set_state(self.prefix + ".discharge_limit_kw", state=self.dp2(discharge_limit_soc), attributes = {'results' : discharge_limit_time_kw, 'friendly_name' : 'Predicted discharge limit kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' :'mdi:battery-charging'})
                self.set_state(self.prefix + ".discharge_limit", state=discharge_limit_percent, attributes = {'results' : discharge_limit_time, 'friendly_name' : 'Predicted discharge limit', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' :'mdi:battery-charging'})
                self.set_state(self.prefix + ".discharge_start", state=discharge_start_str, attributes = {'timestamp' : discharge_start_date, 'friendly_name' : 'Predicted discharge start time', 'state_class': 'measurement', 'state_class': 'timestamp', 'icon': 'mdi:table-clock', 'unit_of_measurement' : None})
                self.set_state(self.prefix + ".discharge_end", state=discharge_end_str, attributes = {'timestamp' : discharge_end_date, 'friendly_name' : 'Predicted discharge end time', 'state_class': 'measurement', 'state_class': 'timestamp', 'icon': 'mdi:table-clock', 'unit_of_measurement' : None})

    def publish_charge_limit(self, charge_limit, charge_window, charge_limit_percent, best):
        """
        Create entity to chart charge limit
        """
        charge_limit_time = {}
        charge_limit_time_kw = {}
        for minute in range(0, self.forecast_minutes + self.minutes_now, 30):
            window = self.in_charge_window(charge_window, minute)
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if window >= 0:
                charge_limit_time[stamp] = charge_limit_percent[window]
                charge_limit_time_kw[stamp] = charge_limit[window]
            else:
                charge_limit_time[stamp] = 0
                charge_limit_time_kw[stamp] = 0
        
        if not SIMULATE:
            charge_limit_first = 0
            charge_limit_percent_first = 0
            charge_start_str = 'undefined'
            charge_end_str = 'undefined'
            charge_start_date = None
            charge_end_date = None

            if charge_limit:
                # Ignore charge windows beyond 24 hours away as they won't apply right now
                if charge_window[0]['end'] <= (24*60 + self.minutes_now):
                    charge_limit_first = charge_limit[0]
                    charge_limit_percent_first = charge_limit_percent[0]
                    charge_start_minutes = charge_window[0]['start']
                    charge_end_minutes = charge_window[0]['end']
                
                    time_format_time = '%H:%M:%S'
                    charge_startt = self.midnight_utc + timedelta(minutes=charge_start_minutes)
                    charge_endt = self.midnight_utc + timedelta(minutes=charge_end_minutes)
                    charge_start_str = charge_startt.strftime(time_format_time)
                    charge_end_str = charge_endt.strftime(time_format_time)
                    charge_start_date = charge_startt.strftime(TIME_FORMAT)
                    charge_end_date = charge_endt.strftime(TIME_FORMAT)

            if best:
                self.set_state(self.prefix + ".best_charge_limit_kw", state=self.dp2(charge_limit_first), attributes = {'results' : charge_limit_time_kw, 'friendly_name' : 'Predicted charge limit kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' :'mdi:battery-charging'})
                self.set_state(self.prefix + ".best_charge_limit", state=charge_limit_percent_first, attributes = {'results' : charge_limit_time, 'friendly_name' : 'Predicted charge limit best', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' :'mdi:battery-charging'})
                self.set_state(self.prefix + ".best_charge_start", state=charge_start_str, attributes = {'timestamp' : charge_start_date, 'friendly_name' : 'Predicted charge start time best', 'state_class': 'measurement', 'state_class': 'timestamp', 'icon': 'mdi:table-clock', 'unit_of_measurement' : None})
                self.set_state(self.prefix + ".best_charge_end", state=charge_end_str, attributes = {'timestamp' : charge_end_date, 'friendly_name' : 'Predicted charge end time best', 'state_class': 'measurement', 'state_class': 'timestamp', 'icon': 'mdi:table-clock', 'unit_of_measurement' : None})
            else:
                self.set_state(self.prefix + ".charge_limit_kw", state=self.dp2(charge_limit_first), attributes = {'results' : charge_limit_time_kw, 'friendly_name' : 'Predicted charge limit kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' :'mdi:battery-charging'})
                self.set_state(self.prefix + ".charge_limit", state=charge_limit_percent_first, attributes = {'results' : charge_limit_time, 'friendly_name' : 'Predicted charge limit', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' :'mdi:battery-charging'})
                self.set_state(self.prefix + ".charge_start", state=charge_start_str, attributes = {'timestamp' : charge_start_date, 'friendly_name' : 'Predicted charge start time', 'state_class': 'measurement', 'state_class': 'timestamp', 'icon': 'mdi:table-clock', 'unit_of_measurement' : None})
                self.set_state(self.prefix + ".charge_end", state=charge_end_str, attributes = {'timestamp' : charge_end_date, 'friendly_name' : 'Predicted charge end time', 'state_class': 'measurement', 'state_class': 'timestamp', 'icon': 'mdi:table-clock', 'unit_of_measurement' : None})

    def reset(self):
        """
        Init stub
        """
        self.prefix = self.args.get('prefix', "predbat")
        self.had_errors = False
        self.prediction_started = False
        self.update_pending = True
        self.midnight = None
        self.midnight_utc = None
        self.difference_minutes = 0
        self.minutes_now = 0
        self.minutes_to_midnight = 0
        self.days_previous = [7]
        self.days_previous_weight = [1]
        self.forecast_days = 0
        self.forecast_minutes = 0
        self.soc_kw = 0
        self.soc_max = 0
        self.predict_soc = {}
        self.predict_soc_best = {}
        self.metric_min_improvement = 0.0
        self.metric_min_improvement_discharge = 0.0
        self.metric_battery_cycle = 0.0
        self.rate_import = {}
        self.rate_export = {}
        self.rate_slots = []
        self.low_rates = []
        self.high_export_rates = []
        self.cost_today_sofar = 0
        self.octopus_slots = []
        self.car_charging_slots = []
        self.reserve = 0
        self.reserve_current = 0
        self.battery_loss = 1.0
        self.battery_loss_discharge = 1.0
        self.inverter_loss = 1.0
        self.inverter_hybrid = True
        self.inverter_soc_reset = False
        self.battery_scaling = 1.0
        self.best_soc_min = 0
        self.best_soc_max = 0
        self.best_soc_margin = 0
        self.best_soc_keep = 0
        self.rate_min = 0
        self.rate_min_minute = 0
        self.rate_min_forward = {}
        self.rate_max = 0
        self.rate_max_minute = 0
        self.rate_export_threshold = 99
        self.rate_threshold = 99
        self.rate_average = 0
        self.rate_export_min = 0
        self.rate_export_min_minute = 0
        self.rate_export_max = 0
        self.rate_export_max_minute = 0
        self.rate_export_average = 0
        self.set_soc_minutes = 0
        self.set_window_minutes = 0
        self.debug_enable = False
        self.import_today = {}
        self.import_today_now = 0
        self.export_today = {}
        self.export_today_now = 0
        self.pv_today = {}
        self.pv_today_now = 0
        self.io_adjusted = {}
        self.current_charge_limit = 0.0
        self.charge_limit = []
        self.charge_limit_percent = []
        self.charge_limit_best = []
        self.charge_limit_best_percent = []
        self.charge_window = []
        self.charge_window_best = []
        self.car_charging_battery_size = [100]
        self.car_charging_limit = [100]
        self.car_charging_soc = [0]
        self.car_charging_rate = [7.4]
        self.car_charging_loss = 1.0
        self.discharge_window = []
        self.discharge_limits = []
        self.discharge_limits_best = []
        self.discharge_window_best = []
        self.battery_rate_max_charge = 0
        self.battery_rate_max_discharge = 0
        self.battery_rate_max_charge_scaled = 0
        self.battery_rate_max_discharge_scaled = 0
        self.charge_rate_max = 0
        self.discharge_rate_max = 0
        self.car_charging_hold = False
        self.car_charging_threshold = 99
        self.car_charging_energy = {}   
        self.simulate_offset = 0
        self.sim_soc = 0
        self.sim_soc_kw = 0
        self.sim_reserve = 4
        self.sim_inverter_mode = "Eco"
        self.sim_charge_start_time = "00:00:00"
        self.sim_charge_end_time = "00:00:00"
        self.sim_discharge_start = "00:00"
        self.sim_discharge_end = "23:59"
        self.sim_charge_schedule_enable = 'on'
        self.sim_charge_rate_max = 2600
        self.sim_discharge_rate_max = 2600
        self.sim_soc_charge = []
        self.notify_devices = ['notify']
        self.octopus_url_cache = {}
        self.ge_url_cache = {}
        self.github_url_cache = {}
        self.load_minutes = {}
        self.load_minutes_now = 0
        self.load_minutes_age = 0
        self.battery_capacity_nominal = False
        self.releases = {}
        self.balance_inverters_enable = False
        self.balance_inverters_charge = True
        self.balance_inverters_discharge = True
        self.balance_inverters_crosscharge = True

    def optimise_charge_limit(self, window_n, record_charge_windows, try_charge_limit, charge_window, discharge_window, discharge_limits, load_minutes_step, pv_forecast_minute_step, pv_forecast_minute10_step, all_n = 0, end_record=None):
        """
        Optimise a single charging window for best SOC
        """
        loop_soc = self.soc_max
        best_soc = self.soc_max
        best_soc_min = 0
        best_soc_min_minute = 0
        best_metric = 9999999
        best_cost = 0
        prev_soc = self.soc_max + 1
        prev_metric = 9999999
        
        # Start the loop at the max soc setting
        if self.best_soc_max > 0:
            loop_soc = min(loop_soc, self.best_soc_max)
        
        while loop_soc >= 0:
            was_debug = self.debug_enable
            self.debug_enable = False

            # Apply user clamping to the value we try
            try_soc = max(self.best_soc_min, loop_soc)
            try_soc = max(try_soc, self.reserve)
            try_soc = self.dp2(min(try_soc, self.soc_max))

            # Stop when we won't change the soc anymore
            if try_soc >= prev_soc:
                self.debug_enable = was_debug
                break

            # Store try value into the window, either all or just this one
            if all_n:
                for window_id in range(0, all_n):
                    try_charge_limit[window_id] = try_soc
            else:
                try_charge_limit[window_n] = try_soc

            # Simulate with medium PV
            metricmid, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle  = self.run_prediction(try_charge_limit, charge_window, discharge_window, discharge_limits, load_minutes_step, pv_forecast_minute_step, end_record = end_record)

            # Simulate with 10% PV 
            metric10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10, battery_cycle10 = self.run_prediction(try_charge_limit, charge_window, discharge_window, discharge_limits, load_minutes_step, pv_forecast_minute10_step, end_record = end_record)

            # Store simulated mid value
            metric = metricmid
            cost = metricmid

            # Balancing payment to account for battery left over 
            # ie. how much extra battery is worth to us in future, assume it's the same as low rate
            rate_min = self.rate_min_forward.get(end_record, self.rate_min)
            metric -= soc * max(rate_min, 1.0) / self.battery_loss
            metric10 -= soc10 * max(rate_min, 1.0) / self.battery_loss

            # Adjustment for battery cycles metric
            metric += battery_cycle * self.metric_battery_cycle
            metric10 += battery_cycle * self.metric_battery_cycle

            # Metric adjustment based on 10% outcome weighting
            if metric10 > metric:
                metric_diff = metric10 - metric
                metric_diff *= self.pv_metric10_weight
                metric += metric_diff
                metric = self.dp2(metric)

            # Metric adjustment based on current charge limit, try to avoid
            # constant changes by weighting the base setting a little
            if window_n == 0:
                if try_soc == self.reserve:
                    try_percent = 0
                else:
                    try_percent = int(try_soc / self.soc_max * 100.0 + 0.5)

                compare_with = max(self.current_charge_limit, self.reserve_current_percent)

                if abs(compare_with - try_percent) <= 2:
                    metric -= max(0.5, self.metric_min_improvement)

            self.debug_enable = was_debug
            if self.debug_enable:
                self.log("Sim: SOC {} window {} imp bat {} house {} exp {} min_soc {} @ {} soc {} cost {} metric {} metricmid {} metric10 {}".format
                        (try_soc, window_n, self.dp2(import_kwh_battery), self.dp2(import_kwh_house), self.dp2(export_kwh), self.dp2(soc_min), self.time_abs_str(soc_min_minute), self.dp2(soc), self.dp2(cost), self.dp2(metric), self.dp2(metricmid), self.dp2(metric10)))

            # Only select the lower SOC if it makes a notable improvement has defined by min_improvement (divided in M windows)
            # and it doesn't fall below the soc_keep threshold 
            if ((metric + self.metric_min_improvement) <= best_metric) and (best_metric==9999999 or (soc_min >= self.best_soc_keep or soc_min >= best_soc_min)):
                best_metric = metric
                best_soc = try_soc
                best_cost = cost
                best_soc_min = soc_min
                best_soc_min_minute = soc_min_minute
            
            prev_soc = try_soc
            prev_metric = metric
            loop_soc -= max(self.best_soc_step, 0.1)

        # Add margin last
        best_soc = min(best_soc + self.best_soc_margin, self.soc_max)

        return best_soc, best_metric, best_cost, best_soc_min, best_soc_min_minute

    def optimise_discharge(self, window_n, record_charge_windows, try_charge_limit, charge_window, discharge_window, discharge_limit, load_minutes_step, pv_forecast_minute_step, pv_forecast_minute10_step, all_n = 0, end_record=None):
        """
        Optimise a single discharging window for best discharge %
        """
        best_discharge = False
        best_metric = 9999999
        best_cost = 0
        best_soc_min = 0
        best_soc_min_minute = 0
        this_discharge_limit = 100.0
        prev_discharge_limit = 0.0
        window = discharge_window[window_n]
        try_discharge_window = copy.deepcopy(discharge_window)
        try_discharge = copy.deepcopy(discharge_limit)
        best_start = window['start']

        # loop on each discharge option
        if self.set_discharge_freeze and not self.set_discharge_freeze_only:
            # If we support freeze, try a 99% option which will freeze at any SOC level below this
            loop_options = [100.0, 99.0, 0.0]
        else:
            loop_options = [100.0, 0.0]

        for loop_limit in loop_options:
            # Loop on window size
            loop_start = window['end'] - 5
            while loop_start >= window['start']:

                this_discharge_limit = loop_limit
                start = loop_start

                # Move the loop start back to full size
                loop_start -= 5

                # Can't optimise all window start slot
                if all_n and (start != window['start']):
                    continue

                # Don't optimise start of disabled windows or freeze only windows, just for discharge ones
                if (this_discharge_limit in [100.0, 99.0]) and (start != window['start']):
                    continue

                # Never go below the minimum level
                this_discharge_limit = max(max(self.best_soc_min, self.reserve) * 100.0 / self.soc_max, this_discharge_limit)
                this_discharge_limit = float(int(this_discharge_limit + 0.5))
                this_discharge_limit = min(this_discharge_limit, 100.0)

                # Store try value into the window
                if all_n:
                    for window_id in range(0, all_n):
                        try_discharge[window_id] = this_discharge_limit
                else:
                    try_discharge[window_n] = this_discharge_limit
                    # Adjust start
                    start = min(start, window['end'] - 5)
                    try_discharge_window[window_n]['start'] = start

                was_debug = self.debug_enable
                self.debug_enable = False

                # Simulate with medium PV
                metricmid, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle  = self.run_prediction(try_charge_limit, charge_window, try_discharge_window, try_discharge, load_minutes_step, pv_forecast_minute_step, end_record = end_record)

                # Simulate with 10% PV 
                metric10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10, battery_cycle10  = self.run_prediction(try_charge_limit, charge_window, try_discharge_window, try_discharge, load_minutes_step, pv_forecast_minute10_step, end_record = end_record)

                # Put back debug enable
                self.debug_enable = was_debug

                # Store simulated mid value
                metric = metricmid
                cost = metricmid

                # Balancing payment to account for battery left over 
                # ie. how much extra battery is worth to us in future, assume it's the same as low rate
                rate_min = self.rate_min_forward.get(end_record, self.rate_min)
                metric -= soc * max(rate_min, 1.0) / self.battery_loss
                metric10 -= soc10 * max(rate_min, 1.0) / self.battery_loss

                # Adjustment for battery cycles metric
                metric += battery_cycle * self.metric_battery_cycle
                metric10 += battery_cycle * self.metric_battery_cycle

                # Metric adjustment based on 10% outcome weighting
                if metric10 > metric:
                    metric_diff = metric10 - metric
                    metric_diff *= self.pv_metric10_weight
                    metric += metric_diff
                    metric = self.dp2(metric)

                # Adjust to try to keep existing windows
                if window_n < 2 and this_discharge_limit < 100.0 and self.discharge_window:
                    pwindow = discharge_window[window_n]
                    dwindow = self.discharge_window[0]
                    if self.minutes_now >= pwindow['start'] and self.minutes_now < pwindow['end']:
                        if (self.minutes_now >= dwindow['start'] and self.minutes_now < dwindow['end']) or (dwindow['end'] == pwindow['start']):
                            self.log("Sim: Discharge window {} - weighting as it falls within currently configured discharge slot (or continues from one)".format(window_n))
                            metric -= max(0.5, self.metric_min_improvement_discharge)

                if self.debug_enable:
                    self.log("Sim: Discharge {} window {} start {} end {}, imp bat {} house {} exp {} min_soc {} @ {} soc {} cost {} metric {} metricmid {} metric10 {} end_record {}".format
                            (this_discharge_limit, window_n, try_discharge_window[window_n]['start'], try_discharge_window[window_n]['end'], self.dp2(import_kwh_battery), self.dp2(import_kwh_house), self.dp2(export_kwh), self.dp2(soc_min), self.time_abs_str(soc_min_minute), self.dp2(soc), self.dp2(cost), self.dp2(metric), self.dp2(metricmid), self.dp2(metric10), end_record))

                # Only select the lower SOC if it makes a notable improvement has defined by min_improvement (divided in M windows)
                # and it doesn't fall below the soc_keep threshold 
                if ((metric + self.metric_min_improvement_discharge) <= best_metric) and (best_metric==9999999 or (soc_min >= self.best_soc_keep or soc_min >= best_soc_min)):
                    best_metric = metric
                    best_discharge = this_discharge_limit
                    best_cost = cost
                    best_soc_min = soc_min
                    best_soc_min_minute = soc_min_minute
                    best_start = start

        return best_discharge, best_start, best_metric, best_cost, best_soc_min, best_soc_min_minute

    def window_sort_func(self, window):
        """
        Helper sort index function
        """
        return float(window['key'])

    def window_sort_func_start(self, window):
        """
        Helper sort index function
        """
        return float(window['start'])

    def sort_window_by_time(self, windows):
        """
        Sort windows in start time order, return a new list of windows
        """
        window_sorted = copy.deepcopy(windows)
        window_sorted.sort(key=self.window_sort_func_start)
        return window_sorted

    def sort_window_by_price(self, windows, reverse_time=False):
        """
        Sort the charge windows by highest price first, return a list of window IDs
        """
        window_with_id = copy.deepcopy(windows)
        wid = 0
        for window in window_with_id:
            window['id'] = wid
            if reverse_time:
                window['key'] = "%04.2f%02d" % (5000 - window['average'], 999 - window['id'])
            else:
                window['key'] = "%04.2f%02d" % (5000 - window['average'], window['id'])
            wid += 1
        window_with_id.sort(key=self.window_sort_func)
        id_list = []
        for window in window_with_id:
            id_list.append(window['id'])
        return id_list

    def remove_intersecting_windows(self, charge_limit_best, charge_window_best, discharge_limit_best, discharge_window_best):
        """
        Filters and removes intersecting charge windows
        """
        new_limit_best = []
        new_window_best = []
        max_slots = len(charge_limit_best)
        max_dslots = len(discharge_limit_best)

        for window_n in range(0, max_slots):
            window = charge_window_best[window_n]
            start = window['start']
            end = window['end']

            clipped = False
            for dwindow_n in range(0, max_dslots):
                dwindow = discharge_window_best[dwindow_n]
                dlimit = discharge_limit_best[dwindow_n]
                dstart = dwindow['start']
                dend = dwindow['end']

                # Overlapping window with enabled discharge?
                if dlimit < 100.0 and dstart < end and dend >= start:
                    if dstart > start:
                        end = dstart
                        clipped = True
                    else:
                        start = dend
                        clipped = True
                
            if (not clipped) or ((end - start) >= 5):
                new_window = {}
                new_window['start'] = start
                new_window['end'] = end
                new_window_best.append(new_window)
                new_limit_best.append(charge_limit_best[window_n])
        return new_limit_best, new_window_best 

    def discard_unused_charge_slots(self, charge_limit_best, charge_window_best, reserve):
        """
        Filter out unused charge slots (those set at reserve)
        """
        new_limit_best = []
        new_window_best = []

        max_slots = len(charge_limit_best)

        for window_n in range(0, max_slots):
            # Only keep slots > than reserve, or keep the last one so we don't have zero slots
            # Also keep a slot if we are already inside it and charging is enabled
            window = charge_window_best[window_n]
            start = window['start']
            end = window['end']
            if (charge_limit_best[window_n] > self.dp2(reserve)) or (self.minutes_now >= start and self.minutes_now < end and self.charge_window and self.charge_window[0]['end'] == end):
                new_limit_best.append(charge_limit_best[window_n])
                new_window_best.append(charge_window_best[window_n])
        return new_limit_best, new_window_best 

    def find_spare_energy(self, predict_soc, predict_export, step, first_charge):
        """
        Find spare energy and set triggers
        """
        triggers = self.args.get('export_triggers', [])
        if not isinstance(triggers, list):
            return

        # Only run if we have export data
        if not predict_export:
            return

        # Check each trigger
        for trigger in triggers:
            total_energy = 0
            name = trigger.get('name', 'trigger')
            minutes = trigger.get('minutes', 60.0)
            minutes = min(max(minutes, 0), first_charge)
            energy = trigger.get('energy', 1.0)
            try:
                energy = float(energy)
            except (ValueError, TypeError):
                energy = 0.0
                self.log("WARN: Bad energy value {} provided via trigger {}".format(energy, name))
                self.record_status("ERROR: Bad energy value {} provided via trigger {}".format(energy, name), had_errors=True)
                
            for minute in range(0, minutes, step):
                total_energy += predict_export[minute]
            sensor_name = "binary_sensor." + self.prefix + "_export_trigger_" + name
            if total_energy >= energy:
                state = 'on'
            else:
                state = 'off'
            self.log("Evalute trigger {} results {} total_energy {}".format(trigger, state, self.dp2(total_energy)))
            self.set_state(sensor_name, state=state, attributes = {'friendly_name' : 'Predbat export trigger ' + name, 'required' : energy, 'available' : self.dp2(total_energy), 'minutes' : minutes, 'icon': 'mdi:clock-start'})

    def clip_charge_slots(self, minutes_now, predict_soc, charge_window_best, charge_limit_best, record_charge_windows, step):
        """
        Clip charge slots that are useless as they don't charge at all
        """
        for window_n in range(0, record_charge_windows):
            window = charge_window_best[window_n]
            limit = charge_limit_best[window_n]
            limit_soc = self.soc_max * limit / 100.0
            window_start = max(window['start'], minutes_now)
            window_end = max(window['end'], minutes_now)
            window_length = window_end - window_start

            if limit <= self.reserve:
                # Ignore disabled windows
                pass
            elif window_length > 0:
                predict_minute_start = int((window_start - minutes_now) / 5) * 5
                predict_minute_end = int((window_end - minutes_now) / 5) * 5

                if (predict_minute_start in predict_soc) and (predict_minute_end in predict_soc):
                    soc_start = predict_soc[predict_minute_start]
                    soc_end = predict_soc[predict_minute_end]
                    soc_min = min(soc_start, soc_end)
                    soc_max = max(soc_start, soc_end)

                    if self.debug_enable:
                        self.log("Examine charge window {} from {} - {} (minute {}) limit {} - starting soc {} ending soc {}".format(window_n, window_start, window_end, predict_minute_start, limit, soc_start, soc_end))

                    if soc_min > charge_limit_best[window_n]:
                        charge_limit_best[window_n] = max(self.reserve, self.best_soc_min)
                        self.log("Clip off charge window {} from {} - {} from limit {} to new limit {}".format(window_n, window_start, window_end, limit, charge_limit_best[window_n]))
                    if soc_max < charge_limit_best[window_n]:
                        limit_soc = min(self.soc_max, soc_max + 10 * self.battery_rate_max_charge_scaled, charge_limit_best[window_n])
                        if self.best_soc_max > 0:
                            limit_soc = min(limit_soc, self.best_soc_max)
                        charge_limit_best[window_n] = max(limit_soc, self.best_soc_min) 
                        self.log("Clip down charge window {} from {} - {} from limit {} to new limit {}".format(window_n, window_start, window_end, limit, charge_limit_best[window_n]))

            else:
                self.log("WARN: Clip charge window {} as it's already passed".format(window_n))
                charge_limit_best[window_n] = max(self.reserve, self.best_soc_min)
        return charge_window_best, charge_limit_best

    def clip_discharge_slots(self, minutes_now, predict_soc, discharge_window_best, discharge_limits_best, record_discharge_windows, step):
        """
        Clip discharge slots to the right length
        """
        for window_n in range(0, record_discharge_windows):
            window = discharge_window_best[window_n]
            limit = discharge_limits_best[window_n]
            limit_soc = self.soc_max * limit / 100.0
            window_start = max(window['start'], minutes_now)
            window_end = max(window['end'], minutes_now)
            window_length = window_end - window_start

            if limit == 100:
                # Ignore disabled windows
                pass
            elif window_length > 0:
                predict_minute_start = int((window_start - minutes_now) / 5) * 5
                predict_minute_end = int((window_end - minutes_now) / 5) * 5
                if (predict_minute_start in predict_soc) and (predict_minute_end in predict_soc):
                    soc_start = predict_soc[predict_minute_start]
                    soc_end = predict_soc[predict_minute_end]
                    soc_min = min(soc_start, soc_end)
                    soc_max = max(soc_start, soc_end)

                    if self.debug_enable:
                        self.log("Examine window {} from {} - {} (minute {}) limit {} - starting soc {} ending soc {}".format(window_n, window_start, window_end, predict_minute_start, limit, soc_start, soc_end))

                    # Discharge level adjustments for safety
                    if soc_min > limit_soc:
                        # Give it 10 minute margin
                        limit_soc = max(limit_soc, soc_min - 10 * self.battery_rate_max_discharge_scaled)
                        discharge_limits_best[window_n] = float(int(limit_soc / self.soc_max * 100.0 + 0.5))
                        if limit != discharge_limits_best[window_n]:
                            self.log("Clip up discharge window {} from {} - {} from limit {} to new limit {}".format(window_n, window_start, window_end, limit, discharge_limits_best[window_n]))
                elif soc_max < limit_soc:
                    # Bring down limit to match predicted soc for freeze only mode
                    if self.set_discharge_freeze:
                        # Get it 5 minute margin upwards
                        limit_soc = min(limit_soc, soc_max + 5 * self.battery_rate_max_discharge_scaled)
                        discharge_limits_best[window_n] = float(int(limit_soc / self.soc_max * 100.0 + 0.5))
                        if limit != discharge_limits_best[window_n]:
                            self.log("Clip down discharge window {} from {} - {} from limit {} to new limit {}".format(window_n, window_start, window_end, limit, discharge_limits_best[window_n]))

            else:
                self.log("WARN: Clip discharge window {} as it's already passed".format(window_n))
                discharge_limits_best[window_n] = 100
        return discharge_window_best, discharge_limits_best

    def discard_unused_discharge_slots(self, discharge_limits_best, discharge_window_best):
        """
        Filter out the windows we disabled
        """
        new_best = []
        new_enable = []
        for window_n in range(0, len(discharge_limits_best)):
            if discharge_limits_best[window_n] < 100.0:
                # Also merge contigous enabled windows
                if new_best and (discharge_window_best[window_n]['start'] == new_best[-1]['end']) and (discharge_limits_best[window_n] == new_enable[-1]):
                    new_best[-1]['end'] = discharge_window_best[window_n]['end']
                    if self.debug_enable:
                        self.log("Combine discharge slot {} with previous - percent {} slot {}".format(window_n, new_enable[-1], new_best[-1]))
                else:
                    new_best.append(copy.deepcopy(discharge_window_best[window_n]))
                    new_enable.append(discharge_limits_best[window_n])

        return new_enable, new_best
    
    def optimise_discharge_windows(self, end_record, load_minutes_step, pv_forecast_minute_step, pv_forecast_minute10_step):
        """
        Optimize the discharge windows
        """

        # Try different discharge options
        if self.discharge_window_best and self.calculate_best_discharge:
            record_discharge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.discharge_window_best), 1)

            # Set all to off
            self.discharge_limits_best = [100.0 for n in range(0, len(self.discharge_window_best))]

            # Optimise in price order, most expensive first try to increase each one
            for discharge_pass in range(0, 1):
                self.log("Optimise discharge pass {}".format(discharge_pass))
                price_sorted = self.sort_window_by_price(self.discharge_window_best[:record_discharge_windows], reverse_time=True)
                for window_n in price_sorted:
                    best_discharge, best_start, best_metric, best_cost, soc_min, soc_min_minute = self.optimise_discharge(window_n, record_discharge_windows, self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, load_minutes_step, pv_forecast_minute_step, pv_forecast_minute10_step, end_record = end_record)

                    self.discharge_limits_best[window_n] = best_discharge
                    self.discharge_window_best[window_n]['start'] = best_start

                    if self.debug_enable or 1:
                        self.log("Best discharge limit window {} time {} - {} discharge {} (adjusted) min {} @ {} (margin added {} and min {}) with metric {} cost {}".format(window_n, self.time_abs_str(self.discharge_window_best[window_n]['start']), self.time_abs_str(self.discharge_window_best[window_n]['end']), best_discharge, self.dp2(soc_min), self.time_abs_str(soc_min_minute), self.best_soc_margin, self.best_soc_min, self.dp2(best_metric), self.dp2(best_cost)))

    def optimise_charge_windows_reset(self, end_record, load_minutes, pv_forecast_minute_step, pv_forecast_minute10_step):
        """
        Reset the charge windows to max
        """
        if self.charge_window_best and self.calculate_best_charge:
            # Set all to max
            self.charge_limit_best = [self.soc_max for n in range(0, len(self.charge_limit_best))]

    def optimise_charge_windows(self, end_record, load_minutes_step, pv_forecast_minute_step, pv_forecast_minute10_step):
        """
        Optimise the charge windows
        """
        if self.charge_window_best and self.calculate_best_charge:
            best_soc = self.soc_max
            record_charge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.charge_window_best), 1)
            self.log("Record charge windows is {} end_record_abs was {}".format(record_charge_windows, self.time_abs_str(end_record + self.minutes_now)))

            if record_charge_windows==1:
                # Set all to min
                self.charge_limit_best = [self.reserve if n < record_charge_windows else self.soc_max for n in range(0, len(self.charge_limit_best))]

                # First do rough optimisation of all windows
                self.log("Optimise all charge windows n={}".format(record_charge_windows))
                best_soc, best_metric, best_cost, soc_min, soc_min_minute = self.optimise_charge_limit(0, record_charge_windows, self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, load_minutes_step, pv_forecast_minute_step, pv_forecast_minute10_step, all_n = record_charge_windows, end_record = end_record)
                if record_charge_windows > 1:
                    best_soc = min(best_soc + self.best_soc_pass_margin, self.soc_max)
                self.log("Best all charge limit all windows n={} (adjusted) soc calculated at {} min {} @ {} (margin added {} and min {} max {}) with metric {} cost {} windows {}".format(record_charge_windows, self.dp2(best_soc), self.dp2(soc_min), self.time_abs_str(soc_min_minute), self.best_soc_margin, self.best_soc_min,  self.best_soc_max, self.dp2(best_metric), self.dp2(best_cost), self.charge_limit_best))

            # Set all to optimisation
            self.charge_limit_best = [best_soc if n < record_charge_windows else self.soc_max for n in range(0, len(self.charge_limit_best))]

            if record_charge_windows > 1:
                for charge_pass in range(0, 1):
                    self.log("Optimise charge pass {}".format(charge_pass))
                    # Optimise in price order, most expensive first try to reduce each one, only required for more than 1 window
                    price_sorted = self.sort_window_by_price(self.charge_window_best[:record_charge_windows], reverse_time=False)
                    for window_n in price_sorted:
                        best_soc, best_metric, best_cost, soc_min, soc_min_minute = self.optimise_charge_limit(window_n, record_charge_windows, self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, load_minutes_step, pv_forecast_minute_step, pv_forecast_minute10_step, end_record = end_record)

                        self.charge_limit_best[window_n] = best_soc
                        if self.debug_enable or 1:
                            self.log("Best charge limit window {} (adjusted) soc calculated at {} min {} @ {} (margin added {} and min {} max {}) with metric {} cost {} windows {}".format(window_n, self.dp2(best_soc), self.dp2(soc_min), self.time_abs_str(soc_min_minute), self.best_soc_margin, self.best_soc_min,  self.best_soc_max, self.dp2(best_metric), self.dp2(best_cost), self.charge_limit_best))


    def window_as_text(self, windows, percents):
        """
        Convert window in minutes to text string
        """
        txt = "{"
        for window_n in range(0, len(windows)):
            window = windows[window_n]
            percent = percents[window_n]
            if window_n > 0:
                txt += ', '
            start_timestamp = self.midnight_utc + timedelta(minutes=window['start'])
            start_time = start_timestamp.strftime("%d-%m %H:%M:%S")
            end_timestamp = self.midnight_utc + timedelta(minutes=window['end'])
            end_time = end_timestamp.strftime("%d-%m %H:%M:%S")
            txt += start_time + ' - '
            txt += end_time
            txt += " @ {}".format(self.dp2(percent))
        txt += '}'
        return txt

    def get_car_charging_planned(self):
        """
        Get the car attributes
        """
        self.car_charging_planned = [False for c in range(0, self.num_cars)]
        self.car_charging_plan_smart  = [False for c in range(0, self.num_cars)]
        self.car_charging_plan_time  = [False for c in range(0, self.num_cars)]
        self.car_charging_battery_size  = [100.0 for c in range(0, self.num_cars)]
        self.car_charging_limit  = [100.0 for c in range(0, self.num_cars)]
        self.car_charging_rate  = [7.4 for c in range(0, self.num_cars)]
        self.car_charging_slots = [[] for c in range(0, self.num_cars)]

        self.car_charging_planned_response = self.get_arg('car_charging_planned_response', ['yes', 'on', 'enable', 'true'])
        for car_n in range(0, self.num_cars):
            # Get car N planned status
            car = self.get_arg('car_charging_planned', "no", index=car_n)            
            if isinstance(car, str):
                if car.lower() in self.car_charging_planned_response:
                    car = True
                else:
                    car = False
            elif not isinstance(car, bool):
                car = False
            self.car_charging_planned[car_n] = car                        
            # Other car related configuration
            self.car_charging_plan_smart[car_n] = self.get_arg('car_charging_plan_smart', False)
            self.car_charging_plan_time[car_n] = self.get_arg('car_charging_plan_time', "07:00:00")
            self.car_charging_battery_size[car_n] = float(self.get_arg('car_charging_battery_size', 100.0, index=car_n))
            self.car_charging_rate[car_n] = (float(self.get_arg('car_charging_rate', 7.4, index=car_n)))
            self.car_charging_limit[car_n]  = (float(self.get_arg('car_charging_limit', 100.0, index=car_n)) * self.car_charging_battery_size[car_n] ) / 100.0

        self.car_charging_from_battery = self.get_arg('car_charging_from_battery', True)
        if self.num_cars > 0:
            self.log("Cars {} charging from battery {} planned {}, smart {}, plan_time {}, battery size {}, limit {}, rate {}".format(self.num_cars, self.car_charging_from_battery, self.car_charging_planned, 
                    self.car_charging_plan_smart, self.car_charging_plan_time, self.car_charging_battery_size, self.car_charging_limit, self.car_charging_rate))

    def fetch_pv_datapoints(self, argname):
        """
        Get some solcast data from argname argument
        """
        data = []
        total_data = 0
        total_sensor = 0

        if argname in self.args:
            # Found out if detailedForcast is present or not, then set the attribute name
            # in newer solcast plugings only forecast is used
            attribute = 'detailedForecast'
            entity_id = self.get_arg(argname, None, indirect=False)
            if entity_id:
                result = self.get_state(entity_id = entity_id, attribute=attribute)
                if not result:
                    attribute = 'forecast'
            try:
                data    = self.get_state(entity_id = self.get_arg(argname, indirect=False), attribute=attribute)
            except (ValueError, TypeError):
                self.log("WARN: Unable to fetch solar forecast data from sensor {} check your setting of {}".format(self.get_arg(argname, indirect=False), argname))                
                self.record_status("Error - {} not be set correctly, check apps.yaml", debug=self.get_arg(argname, indirect=False), had_errors=True)

            # Solcast new vs old version
            # check the total vs the sum of 30 minute slots and work out scale factor
            expected = 0.0
            factor = 1.0
            if data:
                for entry in data:
                    total_data += entry['pv_estimate']
                total_data = self.dp2(total_data)
                total_sensor = self.dp2(self.get_arg(argname, 1.0))
        return data, total_data, total_sensor

    def fetch_pv_forecast(self):
        """
        Fetch the PV Forecast data from Solcast
        """
        pv_forecast_minute = {}
        pv_forecast_minute10 = {}
        pv_forecast_data = []
        pv_forecast_total_data = 0
        pv_forecast_total_sensor = 0

        # Fetch data from each sensor
        for argname in ['pv_forecast_today', 'pv_forecast_tomorrow', 'pv_forecast_d3', 'pv_forecast_d4']:
            data, total_data, total_sensor = self.fetch_pv_datapoints(argname)
            self.log("PV Data for {} total {} kWh".format(argname, total_sensor))
            pv_forecast_data += data
            pv_forecast_total_data += total_data
            pv_forecast_total_sensor += total_sensor

        # Work out data scale factor so it adds up (New Solcast is in kw but old was kWH)
        factor = 1.0
        if pv_forecast_total_data > 0.0:
            factor = self.dp2(pv_forecast_total_data / pv_forecast_total_sensor)
        # We want to divide the data into single minute slots
        divide_by = self.dp2(30 * factor)

        if factor != 1.0 and factor != 2.0:
            self.log("WARN: PV Forecast data adds up to {} kWh but total sensors add up to {} KWh, this is unexpected and hence data maybe misleading".format(pv_forecast_total_data, pv_forecast_total_sensor))

        if pv_forecast_data:
            pv_forecast_minute = self.minute_data(pv_forecast_data, self.forecast_days + 1, self.midnight_utc, 'pv_estimate' + str(self.get_arg('pv_estimate', '')), 'period_start', backwards=False, divide_by=divide_by, scale=self.pv_scaling)
            pv_forecast_minute10 = self.minute_data(pv_forecast_data, self.forecast_days + 1, self.midnight_utc, 'pv_estimate10', 'period_start', backwards=False, divide_by=divide_by, scale=self.pv_scaling)
        else:
            self.log("WARN: No solar data has been configured.")
        
        return pv_forecast_minute, pv_forecast_minute10

    def balance_inverters(self):
        """
        Attempt to balance multiple inverters
        """
        # Charge rate resets
        balance_reset_charge = {}
        balance_reset_discharge = {}

        self.log("BALANCE: Enabled balance charge {} discharge {} crosscharge {}".format(self.balance_inverters_charge, self.balance_inverters_discharge, self.balance_inverters_crosscharge))

        # For each inverter get the details
        skew = self.get_arg('clock_skew', 0)
        local_tz = pytz.timezone(self.get_arg('timezone', "Europe/London"))
        now_utc = datetime.now(local_tz) + timedelta(minutes=skew)
        now = datetime.now() + timedelta(minutes=skew)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        minutes_now = int((now - midnight).seconds / 60)
        num_inverters = int(self.get_arg('num_inverters', 1))
        self.now_utc = now_utc
        self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = int((now - self.midnight).seconds / 60)
        self.minutes_to_midnight = 24*60 - self.minutes_now

        inverters = []
        for id in range(0, num_inverters):
            inverter = Inverter(self, id)
            inverter.update_status(minutes_now)
            inverters.append(inverter)

        out_of_balance = False     # Are all the SOC % the same?
        total_battery_power = 0    # Total battery power across inverters
        total_max_rate = 0         # Total battery max rate across inverters
        total_charge_rates = 0     # Current total charge rates
        total_discharge_rates = 0  # Current total discharge rates
        socs = []
        reserves = []
        battery_powers = []
        battery_max_rates = []
        charge_rates = []
        discharge_rates = []
        for inverter in inverters:
            socs.append(inverter.soc_percent)
            reserves.append(inverter.reserve_current)
            if inverter.soc_percent != inverters[0].soc_percent:
                out_of_balance = True
            battery_powers.append(inverter.battery_power)
            total_battery_power += inverter.battery_power
            battery_max_rates.append(inverter.battery_rate_max_discharge * 60*1000.0)
            total_max_rate += inverter.battery_rate_max_discharge * 60*1000.0
            charge_rates.append(inverter.charge_rate_max * 60*1000.0)
            total_charge_rates += inverter.charge_rate_max * 60*1000.0
            discharge_rates.append(inverter.discharge_rate_max * 60*1000.0)
            total_discharge_rates += inverter.discharge_rate_max * 60*1000.0
        self.log("BALANCE: socs {} reserves {} battery_powers {} total {} battery_max_rates {} charge_rates {} total {} discharge_rates {} total {}".format(socs, reserves, battery_powers, total_battery_power, battery_max_rates, charge_rates, total_charge_rates, discharge_rates, total_discharge_rates))

        # Are we discharging
        during_discharge = total_battery_power >= 0.0
        during_charge = total_battery_power < 0.0

        # Work out min and max socs
        soc_min = min(socs)
        soc_max = max(socs)

        # Work out which inverters have low and high Soc
        soc_low = []
        soc_high = []
        for inverter in inverters:
            soc_low.append(inverter.soc_percent < soc_max)
            soc_high.append(inverter.soc_percent > soc_min)
        
        above_reserve = [] # Are the inverters above the reserve
        can_power_house = [] # Could this inverter power the house alone?
        power_enough_discharge = [] # Inverter drawing enough power to be worth balancing
        power_enough_charge = []    # Inverter drawing enough power to be worth balancing
        for id in range(0, num_inverters):
            above_reserve.append((socs[id] - reserves[id]) >= 4.0)
            can_power_house.append((total_discharge_rates - discharge_rates[id] - 200) >= total_battery_power)
            power_enough_discharge.append(battery_powers[id] >= 50.0)
            power_enough_charge.append(inverters[id].battery_power <= -50.0)

        self.log("BALANCE: out_of_balance {} above_reserve {} can_power_house {} power_enough_discharge {} power_enough_charge {} soc_low {} soc_high {}".format(out_of_balance, above_reserve, can_power_house, power_enough_discharge, power_enough_charge, soc_low, soc_high))
        for this_inverter in range(0, num_inverters):
            other_inverter = (this_inverter + 1) % num_inverters
            if self.balance_inverters_discharge and total_discharge_rates > 0 and out_of_balance and during_discharge and soc_low[this_inverter] and power_enough_discharge[this_inverter] and above_reserve[other_inverter] and can_power_house[this_inverter]:
                self.log("BALANCE: Inverter {} is out of balance low - during discharge, attempting to balance it using inverter {}".format(this_inverter, other_inverter))
                balance_reset_discharge[id] = True
                inverters[this_inverter].adjust_discharge_rate(0)
            elif self.balance_inverters_charge and total_charge_rates > 0 and out_of_balance and during_charge and soc_high[this_inverter] and power_enough_charge[this_inverter]:
                self.log("BALANCE: Inverter {} is out of balance high - during charge, attempting to balance it".format(this_inverter))
                balance_reset_charge[id] = True
                inverters[this_inverter].adjust_charge_rate(0)
            elif self.balance_inverters_crosscharge and during_discharge and total_discharge_rates > 0 and power_enough_charge[this_inverter]:
                self.log("BALANCE: Inverter {} is cross charging during discharge, attempting to balance it".format(this_inverter))
                balance_reset_charge[id] = True
                inverters[this_inverter].adjust_charge_rate(0)
            elif self.balance_inverters_crosscharge and during_charge and total_charge_rates > 0 and power_enough_discharge[this_inverter]:
                self.log("BALANCE: Inverter {} is cross discharging during charge, attempting to balance it".format(this_inverter))
                balance_reset_charge[id] = True
                inverters[this_inverter].adjust_charge_rate(0)

        for id in range(0, num_inverters):
            if not balance_reset_charge.get(id, False) and total_charge_rates != 0 and charge_rates[id]==0:
                self.log("BALANCE: Inverter {} reset charge rate to {} now balanced".format(id, inverter.charge_rate_max*60*1000))
                inverters[id].adjust_charge_rate(inverter.charge_rate_max*60*1000)
            if not balance_reset_discharge.get(id, False) and total_discharge_rates != 0 and discharge_rates[id]==0:
                self.log("BALANCE: Inverter {} reset discharge rate to {} now balanced".format(id, inverter.discharge_rate_max*60*1000))
                inverters[id].adjust_discharge_rate(inverter.discharge_rate_max*60*1000)
        
        self.log("BALANCE: Completed this run")

    def update_pred(self, scheduled=True):
        """
        Update the prediction state, everything is called from here right now
        """
        self.had_errors = False
        local_tz = pytz.timezone(self.get_arg('timezone', "Europe/London"))
        skew = self.get_arg('clock_skew', 0)
        if skew:
            self.log("WARN: Clock skew is set to {} minutes".format(skew))
        now_utc = datetime.now(local_tz) + timedelta(minutes=skew)
        now = datetime.now() + timedelta(minutes=skew)
        if SIMULATE:
            now += timedelta(minutes=self.simulate_offset)
            now_utc += timedelta(minutes=self.simulate_offset)

        self.log("--------------- PredBat - update at {} with clock skew {} minutes".format(now_utc, skew))

        self.download_predbat_releases()
        self.expose_config('version', None)

        self.debug_enable = self.get_arg('debug_enable', False)
        self.max_windows = self.get_arg('max_windows', 128)
        self.num_cars = self.get_arg('num_cars', 1)

        self.log("Debug enable is {}".format(self.debug_enable))

        self.now_utc = now_utc
        self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        self.difference_minutes = self.minutes_since_yesterday(now)
        self.minutes_now = int((now - self.midnight).seconds / 60)
        self.minutes_to_midnight = 24*60 - self.minutes_now

        # Days previous
        self.holiday_days_left = self.get_arg('holiday_days_left', 0)
        self.days_previous = self.get_arg('days_previous', [7])
        self.days_previous_weight = self.get_arg('days_previous_weight', [1 for i in range(0, len(self.days_previous))])
        if len(self.days_previous) > len(self.days_previous_weight):
            # Extend weights with 1 if required
            self.days_previous_weight += [1 for i in range(0, len(self.days_previous) - len(self.days_previous_weight))]
        if self.holiday_days_left > 0:
            self.days_previous = [1]
            self.log("Holiday mode is active, {} days remaining, setting days previous to 1".format(self.holiday_days_left))
        self.max_days_previous = max(self.days_previous) + 1

        forecast_hours = self.get_arg('forecast_hours', 48)
        self.forecast_days = int((forecast_hours + 23)/24)
        self.forecast_minutes = forecast_hours * 60
        self.forecast_plan_hours = self.get_arg('forecast_plan_hours', 24)
        self.inverter_clock_skew_start = self.get_arg('inverter_clock_skew_start', 0)
        self.inverter_clock_skew_end = self.get_arg('inverter_clock_skew_end', 0)
        self.inverter_clock_skew_discharge_start = self.get_arg('inverter_clock_skew_discharge_start', 0)
        self.inverter_clock_skew_discharge_end = self.get_arg('inverter_clock_skew_discharge_end', 0)

        # Log clock skew
        if self.inverter_clock_skew_start != 0 or self.inverter_clock_skew_end != 0:
            self.log("Inverter clock skew start {} end {} applied".format(self.inverter_clock_skew_start, self.inverter_clock_skew_end))
        if self.inverter_clock_skew_discharge_start != 0 or self.inverter_clock_skew_discharge_end != 0:
            self.log("Inverter clock skew discharge start {} end {} applied".format(self.inverter_clock_skew_discharge_start, self.inverter_clock_skew_discharge_end))

        # Metric config
        self.metric_min_improvement = self.get_arg('metric_min_improvement', 0.0)
        self.metric_min_improvement_discharge = self.get_arg('metric_min_improvement_discharge', 0.1)
        self.metric_battery_cycle = self.get_arg('metric_battery_cycle', 0.0)
        self.notify_devices = self.get_arg('notify_devices', ['notify'])
        self.pv_scaling = self.get_arg('pv_scaling', 1.0)
        self.pv_metric10_weight = self.get_arg('pv_metric10_weight', 0.15)
        self.load_scaling = self.get_arg('load_scaling', 1.0)
        self.battery_rate_max_scaling = self.get_arg('battery_rate_max_scaling', 1.0)
        self.best_soc_pass_margin = self.get_arg('best_soc_pass_margin', 0.0)
        self.rate_low_threshold = self.get_arg('rate_low_threshold', 0.8)
        self.rate_high_threshold = self.get_arg('rate_high_threshold', 1.2)
        self.rate_low_match_export = self.get_arg('rate_low_match_export', False)
        self.best_soc_step = self.get_arg('best_soc_step', 0.25)

        # Battery charging options
        self.battery_capacity_nominal = self.get_arg('battery_capacity_nominal', False)
        self.battery_loss = 1.0 - self.get_arg('battery_loss', 0.05)
        self.battery_loss_discharge = 1.0 - self.get_arg('battery_loss_discharge', 0.05)
        self.inverter_loss = 1.0 - self.get_arg('inverter_loss', 0.00)
        self.inverter_hybrid = self.get_arg('inverter_hybrid', True)
        self.inverter_soc_reset = self.get_arg('inverter_soc_reset', False)
        self.battery_scaling = self.get_arg('battery_scaling', 1.0)
        self.battery_charge_power_curve = self.args.get('battery_charge_power_curve', {})
        # Check power curve is a dictionary
        if not isinstance(self.battery_charge_power_curve, dict):
            self.battery_charge_power_curve = {}
            self.log("WARN: battery_power_curve is incorrectly configured - ignoring")
            self.record_status("battery_power_curve is incorrectly configured - ignoring", had_errors=True)
        self.import_export_scaling = self.get_arg('import_export_scaling', 1.0)
        self.best_soc_margin = self.get_arg('best_soc_margin', 0.0)
        self.best_soc_min = self.get_arg('best_soc_min', 0.0)
        self.best_soc_max = self.get_arg('best_soc_max', 0.0)
        self.best_soc_keep = self.get_arg('best_soc_keep', 2.0)
        self.set_soc_minutes = self.get_arg('set_soc_minutes', 30)
        self.set_window_minutes = self.get_arg('set_window_minutes', 30)
        self.octopus_intelligent_charging = self.get_arg('octopus_intelligent_charging', True)
        self.get_car_charging_planned()
       
        self.combine_mixed_rates = self.get_arg('combine_mixed_rates', False)
        self.combine_discharge_slots = self.get_arg('combine_discharge_slots', False)
        self.combine_charge_slots = self.get_arg('combine_charge_slots', True)
        self.discharge_slot_split = 30
        self.charge_slot_split = 30

        # Enables
        self.calculate_best = self.get_arg('calculate_best', True)
        self.set_soc_enable = self.get_arg('set_soc_enable', True)
        self.set_reserve_enable = self.get_arg('set_reserve_enable', True)
        self.set_reserve_notify = self.get_arg('set_reserve_notify', True)
        self.set_reserve_hold   = self.get_arg('set_reserve_hold', True)
        self.set_soc_notify = self.get_arg('set_soc_notify', True)
        self.set_window_notify = self.get_arg('set_window_notify', True)
        self.set_charge_window = self.get_arg('set_charge_window', True)
        self.set_discharge_window = self.get_arg('set_discharge_window', True)
        self.set_discharge_freeze = self.get_arg('set_discharge_freeze', True)
        self.set_discharge_freeze_only = self.get_arg('set_discharge_freeze_only', False)
        self.set_discharge_notify = self.get_arg('set_discharge_notify', True)
        self.calculate_best_charge = self.get_arg('calculate_best_charge', True)
        self.calculate_best_discharge = self.get_arg('calculate_best_discharge', True)
        self.calculate_discharge_first = self.get_arg('calculate_discharge_first', True)
        self.balance_inverters_enable = self.get_arg('balance_inverters_enable', False)
        self.balance_inverters_charge = self.get_arg('balance_inverters_charge', True)
        self.balance_inverters_discharge = self.get_arg('balance_inverters_discharge', True)
        self.balance_inverters_crosscharge = self.get_arg('balance_inverters_crosscharge', True)

        # Enable load filtering
        self.load_filter_modal = self.get_arg('load_filter_modal', False)

        # Iboost model
        self.iboost_enable = self.get_arg('iboost_enable', False)
        self.iboost_max_energy = self.get_arg('iboost_max_energy', 3.0)
        self.iboost_max_power = self.get_arg('iboost_max_power', 2400) / 1000 / 60.0
        self.iboost_min_power = self.get_arg('iboost_min_power', 500)  / 1000 / 60.0
        self.iboost_min_soc = self.get_arg('iboost_min_soc', 0.0)
        self.iboost_today = self.get_arg('iboost_today', 0.0)
        self.iboost_next = self.iboost_today
        self.iboost_energy_scaling = self.get_arg('iboost_energy_scaling', 1.0)
        self.iboost_energy_today = {}

        # Car options
        self.car_charging_hold = self.get_arg('car_charging_hold', True)
        self.car_charging_threshold = float(self.get_arg('car_charging_threshold', 6.0)) / 60.0
        self.car_charging_energy_scale = self.get_arg('car_charging_energy_scale', 1.0)

        self.rate_import = {}
        self.rate_export = {}
        self.rate_slots = []
        self.io_adjusted = {}
        self.low_rates = []
        self.high_export_rates = []
        self.octopus_slots = []
        self.cost_today_sofar = 0
        self.import_today = {}
        self.export_today = {}
        self.pv_today = {}
        self.load_minutes = {}
        self.load_minutes_age = 0

        # Iboost load data
        if self.iboost_enable:
            if 'iboost_energy_today' in self.args:
                self.iboost_energy_today, iboost_energy_age = self.minute_data_load(now_utc, 'iboost_energy_today', 1)
                if iboost_energy_age >= 1:
                    self.iboost_today = self.dp2(abs(self.iboost_energy_today[0] - self.iboost_energy_today[self.minutes_now]))
                    self.log("IBoost energy today from sensor reads {} kwh".format(self.iboost_today))

        # Load previous load data
        if self.get_arg('ge_cloud_data', False):
            self.download_ge_data(now_utc)
        else:
            # Load data
            if 'load_today' in self.args:
                self.load_minutes, self.load_minutes_age = self.minute_data_load(now_utc, 'load_today', self.max_days_previous)
                self.log("Found {} load_today datapoints going back {} days".format(len(self.load_minutes), self.load_minutes_age))
                self.load_minutes_now = self.get_arg('load_today', 0.0, combine=True)
            else:
                self.log("WARN: You have not set load_today, you will have no load data")
                self.record_status(message="Error - load_today not set correctly", had_errors=True)

            # Load import today data 
            if 'import_today' in self.args:
                self.import_today = self.minute_data_import_export(now_utc, 'import_today')
                self.import_today_now = self.get_arg('import_today', 0.0, combine=True)
            else:
                self.log("WARN: You have not set import_today in apps.yaml, you will have no previous import data")

            # Load export today data 
            if 'export_today' in self.args:
                self.export_today = self.minute_data_import_export(now_utc, 'export_today')
                self.export_today_now = self.get_arg('export_today', 0.0, combine=True)
            else:
                self.log("WARN: You have not set export_today in apps.yaml, you will have no previous export data")

            # PV today data 
            if 'pv_today' in self.args:
                self.pv_today = self.minute_data_import_export(now_utc, 'pv_today')
                self.pv_today_now = self.get_arg('pv_today', 0.0, combine=True)
            else:
                self.log("WARN: You have not set pv_today in apps.yaml, you will have no previous pv data")

        # Log current values
        self.log("Current data so far today: load {} kWh import {} kWh export {} kWh pv {} kWh".format(self.dp2(self.load_minutes_now), self.dp2(self.import_today_now), self.dp2(self.export_today_now), self.dp2(self.pv_today_now)))
        
        if 'rates_import_octopus_url' in self.args:
            # Fixed URL for rate import
            self.log("Downloading import rates directly from url {}".format(self.get_arg('rates_import_octopus_url', indirect=False)))
            self.rate_import = self.download_octopus_rates(self.get_arg('rates_import_octopus_url', indirect=False))
        elif 'metric_octopus_import' in self.args:
            # Octopus import rates
            entity_id = self.get_arg('metric_octopus_import', None, indirect=False)
            data_all = []
            
            if entity_id:
                data_import = self.get_state(entity_id = entity_id, attribute='rates')
                if data_import:
                    data_all += data_import
                else:
                    data_import = self.get_state(entity_id = entity_id, attribute='all_rates')
                    if data_import:
                        data_all += data_import

            if data_all:
                rate_key = 'rate'
                from_key = 'from'
                to_key = 'to'
                if rate_key not in data_all[0]:
                    rate_key = 'value_inc_vat'
                    from_key = 'valid_from'
                    to_key = 'valid_to'
                self.rate_import = self.minute_data(data_all, self.forecast_days + 1, self.midnight_utc, rate_key, from_key, backwards=False, to_key=to_key, adjust_key='is_intelligent_adjusted')
            else:
                self.log("Warning: metric_octopus_import is not set correctly, ignoring..")
                self.record_status(message="Error - metric_octopus_import not set correctly", had_errors=True)
        else:
            # Basic rates defined by user over time
            self.rate_import = self.basic_rates(self.get_arg('rates_import', [], indirect=False), 'import')

        # Work out current car SOC and limit
        self.car_charging_loss = 1 - float(self.get_arg('car_charging_loss', 0.08))

        # Octopus intelligent slots
        if 'octopus_intelligent_slot' in self.args:
            completed = []
            planned = []
            vehicle = {}
            vehicle_pref = {}
            entity_id = self.get_arg('octopus_intelligent_slot', indirect=False)
            try:
                completed = self.get_state(entity_id = entity_id, attribute='completedDispatches')
                if not completed:
                    completed = self.get_state(entity_id = entity_id, attribute='completed_dispatches')
                planned = self.get_state(entity_id = entity_id, attribute='plannedDispatches')
                if not planned:
                    planned = self.get_state(entity_id = entity_id, attribute='planned_dispatches')
                vehicle = self.get_state(entity_id = entity_id, attribute='registeredKrakenflexDevice')
                vehicle_pref = self.get_state(entity_id = entity_id, attribute='vehicleChargingPreferences')            
            except (ValueError, TypeError):
                self.log("WARN: Unable to get data from {} - octopus_intelligent_slot may not be set correctly".format(entity_id))
                self.record_status(message="Error - octopus_intelligent_slot not set correctly", had_errors=True)

            # Completed and planned slots
            if completed:
                self.octopus_slots += completed
            if planned:
                self.octopus_slots += planned

            # Get rate for import to compute charging costs
            if self.rate_import:
                self.rate_import = self.rate_scan(self.rate_import, print=False)

            if self.num_cars >= 1:
                # Extract vehicle data if we can get it            
                if vehicle:
                    self.car_charging_battery_size[0] = float(vehicle.get('vehicleBatterySizeInKwh', self.car_charging_battery_size[0]))
                    self.car_charging_rate[0] = float(vehicle.get('chargePointPowerInKw', self.car_charging_rate[0]))
                else:
                    size = self.get_state(entity_id = entity_id, attribute='vehicle_battery_size_in_kwh')
                    rate = self.get_state(entity_id = entity_id, attribute='charge_point_power_in_kw')
                    if size:
                        self.car_charging_battery_size[0] = size
                    if rate:
                        self.car_charging_rate[0] = rate

                # Get car charging limit again from car based on new battery size
                self.car_charging_limit[0] = (float(self.get_arg('car_charging_limit', 100.0, index=0)) * self.car_charging_battery_size[0]) / 100.0

                # Extract vehicle preference if we can get it
                if vehicle_pref and self.octopus_intelligent_charging:
                    octopus_limit = max(float(vehicle_pref.get('weekdayTargetSoc', 100)), float(vehicle_pref.get('weekendTargetSoc', 100)))
                    octopus_ready_time = vehicle_pref.get('weekdayTargetTime', None)
                    if not octopus_ready_time:
                        octopus_ready_time = self.car_charging_plan_time[0]
                    else:
                        octopus_ready_time += ":00"
                    self.car_charging_plan_time[0] = octopus_ready_time
                    octopus_limit = self.dp2(octopus_limit * self.car_charging_battery_size[0] / 100.0)
                    self.car_charging_limit[0] = min(self.car_charging_limit[0], octopus_limit)
                elif self.octopus_intelligent_charging:
                    octopus_ready_time = self.get_arg('octopus_ready_time', None)
                    octopus_limit = self.get_arg('octopus_charge_limit', None)
                    if octopus_limit:
                        octopus_limit = self.dp2(float(octopus_limit) * self.car_charging_battery_size[0] / 100.0)
                        self.car_charging_limit[0] = min(self.car_charging_limit[0], octopus_limit)
                    if octopus_ready_time:
                        self.car_charging_plan_time[0] = octopus_ready_time
                
                # Use octopus slots for charging?
                if self.octopus_intelligent_charging:
                    self.car_charging_slots[0] = self.load_octopus_slots(self.octopus_slots)
                self.log("Car 0 using Octopus, charging limit {}, ready time {} - battery size {}".format(self.car_charging_limit[0], self.car_charging_plan_time[0], self.car_charging_battery_size[0]))
        else:
            # Disable octopus charging if we don't have the slot sensor
            self.octopus_intelligent_charging = False

        # Work out car SOC
        self.car_charging_soc = [0.0 for car_n in range(0, self.num_cars)]
        for car_n in range(0, self.num_cars):
            self.car_charging_soc[car_n] = (self.get_arg('car_charging_soc', 0.0, index=car_n) * self.car_charging_battery_size[car_n]) / 100.0
        if self.num_cars:
            self.log("Current Car SOC kwh: {}".format(self.car_charging_soc))

        if 'rates_export_octopus_url' in self.args:
            # Fixed URL for rate export
            self.log("Downloading export rates directly from url {}".format(self.get_arg('rates_export_octopus_url', indirect=False)))
            self.rate_export = self.download_octopus_rates(self.get_arg('rates_export_octopus_url', indirect=False))
        elif 'metric_octopus_export' in self.args:
            # Octopus export rates
            entity_id = self.get_arg('metric_octopus_export', None, indirect=False)
            data_all_export = []

            data_export = self.get_state(entity_id = entity_id, attribute='rates')
            if data_export:
                data_all_export += data_export
            else:
                data_export = self.get_state(entity_id = entity_id, attribute='all_rates')
                if data_export:
                    data_all_export += data_export
                    
            if data_all_export:
                rate_key = 'rate'
                from_key = 'from'
                to_key = 'to'
                if rate_key not in data_all_export[0]:
                    rate_key = 'value_inc_vat'
                    from_key = 'valid_from'
                    to_key = 'valid_to'
                self.rate_export = self.minute_data(data_all_export, self.forecast_days + 1, self.midnight_utc, rate_key, from_key, backwards=False, to_key=to_key)
            else:
                self.log("Warning: metric_octopus_export is not set correctly, ignoring..")
                self.record_status(message="Error - metric_octopus_export not set correctly", had_errors=True)
        else:
            # Basic rates defined by user over time
            self.rate_export = self.basic_rates(self.get_arg('rates_export', [], indirect=False), 'export')

        # Standing charge
        self.metric_standing_charge = self.get_arg('metric_standing_charge', 0.0) * 100.0
        self.log("Standing charge is set to {} p".format(self.metric_standing_charge))

        # Replicate and scan import rates
        if self.rate_import:
            self.rate_import = self.rate_scan(self.rate_import, print=False)
            self.rate_import = self.rate_replicate(self.rate_import, self.io_adjusted)
            self.rate_import = self.rate_add_io_slots(self.rate_import, self.octopus_slots)
            if 'rates_import_override' in self.args:
                self.rate_import = self.basic_rates(self.get_arg('rates_import_override', [], indirect=False), 'import', self.rate_import)
            self.rate_import = self.rate_scan(self.rate_import, print=True)
        else:
            self.log("Warning: No import rate data provided")
            self.record_status(message="Error - No import rate data provided", had_errors=True)

        # Replicate and scan export rates
        if self.rate_export:
            self.rate_export = self.rate_replicate(self.rate_export)
            if 'rates_export_override' in self.args:
                self.rate_export = self.basic_rates(self.get_arg('rates_export_override', [], indirect=False), 'export', self.rate_export)
            self.rate_export = self.rate_scan_export(self.rate_export, print=True)
        else:
            self.log("Warning: No export rate data provided")
            self.record_status(message="Error - No export rate data provided", had_errors=True)

        # Set rate thresholds
        if self.rate_import or self.rate_export:
            self.set_rate_thresholds()

        # Find discharging windows
        if self.rate_export:
            self.high_export_rates = self.rate_scan_window(self.rate_export, 5, self.rate_export_threshold, True)
            self.publish_rates(self.rate_export, True)

        # Find charging windows
        if self.rate_import:
            # Find charging window
            self.low_rates = self.rate_scan_window(self.rate_import, 5, self.rate_threshold, False)
            self.publish_rates(self.rate_import, False)

        # Work out car plan?
        for car_n in range(0, self.num_cars):
            if self.octopus_intelligent_charging and car_n == 0:
                self.log("Car 0 is using Octopus intelligent schedule")
            elif self.car_charging_planned[car_n] :
                self.log("Plan car {} charging from {} to {} with slots {} from soc {} to {} ready by {}".format(car_n, self.car_charging_soc[car_n], self.car_charging_limit[car_n], self.low_rates, self.car_charging_soc[car_n], self.car_charging_limit[car_n], self.car_charging_plan_time[car_n]))
                self.car_charging_slots[car_n] = self.plan_car_charging(car_n, self.low_rates)
            else:
                self.log("Not planning car charging for car {} - car charging planned is False".format(car_n))

            # Log the charging plan
            if self.car_charging_slots[car_n]:
                self.log("Car {} charging plan is: {}".format(car_n, self.car_charging_slots[car_n]))

        # Publish the car plan
        self.publish_car_plan()

        # Work out cost today
        if self.import_today:
            self.cost_today_sofar = self.today_cost(self.import_today, self.export_today)

        # Find the inverters
        self.num_inverters = int(self.get_arg('num_inverters', 1))
        self.inverter_limit = 0.0
        self.export_limit = 0.0
        self.inverters = []
        self.charge_window = []
        self.discharge_window = []
        self.discharge_limits = []
        self.current_charge_limit = 0.0
        self.soc_kw = 0.0
        self.soc_max = 0.0
        self.reserve = 0.0
        self.reserve_current = 0.0
        self.reserve_current_precent = 0.0
        self.battery_rate_max_charge = 0.0
        self.battery_rate_max_discharge = 0.0
        self.battery_rate_max_charge_scaled = 0.0
        self.battery_rate_max_discharge_scaled = 0.0
        self.charge_rate_max = 0.0
        self.discharge_rate_max = 0.0
        found_first = False

        # For each inverter get the details
        for id in range(0, self.num_inverters):
            inverter = Inverter(self, id)
            inverter.update_status(self.minutes_now)

            # As the inverters will run in lockstep, we will initially look at the programming of the first enabled one for the current window setting
            if not found_first:
                found_first = True
                self.current_charge_limit = inverter.current_charge_limit
                self.charge_window = inverter.charge_window
                self.discharge_window = inverter.discharge_window
                self.discharge_limits = inverter.discharge_limits
            self.soc_max += inverter.soc_max
            self.soc_kw += inverter.soc_kw
            self.reserve += inverter.reserve
            self.reserve_current += inverter.reserve_current
            self.battery_rate_max_charge += inverter.battery_rate_max_charge
            self.battery_rate_max_discharge += inverter.battery_rate_max_discharge
            self.battery_rate_max_charge_scaled += inverter.battery_rate_max_charge_scaled
            self.battery_rate_max_discharge_scaled += inverter.battery_rate_max_discharge_scaled
            self.charge_rate_max += inverter.charge_rate_max
            self.discharge_rate_max += inverter.discharge_rate_max
            self.inverters.append(inverter)
            self.inverter_limit += inverter.inverter_limit
            self.export_limit += inverter.export_limit

        # Remove extra decimals
        self.soc_max = self.dp2(self.soc_max)
        self.soc_kw = self.dp2(self.soc_kw)
        self.reserve_current = self.dp2(self.reserve_current)
        self.reserve_current_percent = int(self.reserve_current / self.soc_max * 100.0 + 0.5)

        self.log("Found {} inverters totals: min reserve {} current reserve {} soc_max {} soc {} charge rate {} kw discharge rate {} kw ac limit {} export limit {} kw loss charge {} % loss discharge {} % inverter loss {} %".format(
                 len(self.inverters), self.reserve, self.reserve_current, self.soc_max, self.soc_kw, self.charge_rate_max * 60, self.discharge_rate_max * 60, self.dp2(self.inverter_limit * 60), self.dp2(self.export_limit * 60), 100 - int(self.battery_loss * 100), 100 - int(self.battery_loss_discharge * 100), 100 - int(self.inverter_loss * 100)))

        # Work out current charge limits
        self.charge_limit = [self.current_charge_limit * self.soc_max / 100.0 for i in range(0, len(self.charge_window))]
        self.charge_limit_percent = [self.current_charge_limit for i in range(0, len(self.charge_window))]

        self.log("Base charge    window {}".format(self.window_as_text(self.charge_window, self.charge_limit)))
        self.log("Base discharge window {}".format(self.window_as_text(self.discharge_window, self.discharge_limits)))

        # Calculate best charge windows
        if self.low_rates:
            # If we are using calculated windows directly then save them
            self.charge_window_best = copy.deepcopy(self.low_rates)
        else:
            # Default best charge window as this one
            self.charge_window_best = self.charge_window

        if self.set_soc_enable and not self.set_charge_window:
            # If we can't control the charge window, but we can control the SOC then don't calculate a new window or the calculated SOC will be wrong
            self.log("Note: Set SOC is enabled, but set charge window is disabled, so using the existing charge window only")
            self.charge_window_best = self.charge_window

        # Calculate best discharge windows
        if self.high_export_rates:
            self.discharge_window_best = copy.deepcopy(self.high_export_rates)
        else:
            self.discharge_window_best = []

        # Pre-fill best charge limit with the current charge limit
        self.charge_limit_best = [self.current_charge_limit * self.soc_max / 100.0 for i in range(0, len(self.charge_window_best))]
        self.charge_limit_percent_best = [self.current_charge_limit for i in range(0, len(self.charge_window_best))]

        # Pre-fill best discharge enable with Off
        self.discharge_limits_best = [100.0 for i in range(0, len(self.discharge_window_best))]

        # Show best windows
        self.log('Best charge    window {}'.format(self.window_as_text(self.charge_window_best, self.charge_limit_best)))
        self.log('Best discharge window {}'.format(self.window_as_text(self.discharge_window_best, self.discharge_limits_best)))

        # Fetch PV forecast if enbled, today must be enabled, other days are optional
        pv_forecast_minute, pv_forecast_minute10 = self.fetch_pv_forecast()

        # Car charging hold - when enabled battery is held during car charging in simulation
        self.car_charging_energy = {}
        if 'car_charging_energy' in self.args:
            history = []
            try:
                history = self.get_history(entity_id = self.get_arg('car_charging_energy', indirect=False), days = self.max_days_previous)
            except (ValueError, TypeError):
                self.log("WARN: Unable to fetch history from sensor {} - car_charging_energy may not be set correctly".format(self.get_arg('car_charging_energy', indirect=False)))
                self.record_status("Error - car_charging_energy not be set correctly", debug=self.get_arg('car_charging_energy', indirect=False), had_errors=True)

            if history:
                self.car_charging_energy = self.minute_data(history[0], self.max_days_previous, now_utc, 'state', 'last_updated', backwards=True, smoothing=True, clean_increment=True, scale=self.car_charging_energy_scale)
                self.log("Car charging hold {} with energy data".format(self.car_charging_hold))
        else:
            self.log("Car charging hold {} threshold {}".format(self.car_charging_hold, self.car_charging_threshold*60.0))

        # Apply modal filter to historical data
        self.previous_days_modal_filter(self.load_minutes)
        self.log("Historical days now {} weight {}".format(self.days_previous, self.days_previous_weight))

        # Create step data for car charging energy
        self.car_charging_energy_step = {}
        if self.car_charging_energy:
            self.car_charging_energy_step = self.step_data_history(self.car_charging_energy, self.minutes_now, forward=False)

        # Created optimised step data
        load_minutes_step = self.step_data_history(self.load_minutes, self.minutes_now, forward=False)
        pv_forecast_minute_step = self.step_data_history(pv_forecast_minute, self.minutes_now, forward=True)
        pv_forecast_minute10_step = self.step_data_history(pv_forecast_minute10, self.minutes_now, forward=True)

        # Simulate current settings
        end_record = self.record_length(self.charge_window_best)
        metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle  = self.run_prediction(self.charge_limit, self.charge_window, self.discharge_window, self.discharge_limits, load_minutes_step, pv_forecast_minute_step, save='base', end_record=end_record)
        metricb10, import_kwh_batteryb10, import_kwh_houseb10, export_kwhb10, soc_minb10, socb10, soc_min_minuteb10, battery_cycle10  = self.run_prediction(self.charge_limit, self.charge_window, self.discharge_window, self.discharge_limits, load_minutes_step, pv_forecast_minute10_step, save='base10', end_record=end_record)

        # Publish charge limit base
        self.charge_limit_percent = self.calc_percent_limit(self.charge_limit)
        self.publish_charge_limit(self.charge_limit, self.charge_window, self.charge_limit_percent, best=False)

        # Try different battery SOCs to get the best result
        if self.calculate_best:
            if self.calculate_discharge_first:
                self.log("Calculate discharge first is set")
                self.optimise_charge_windows_reset(end_record, load_minutes_step, pv_forecast_minute_step, pv_forecast_minute10_step)
                self.optimise_discharge_windows(end_record, load_minutes_step, pv_forecast_minute_step, pv_forecast_minute10_step)
                self.optimise_charge_windows(end_record, load_minutes_step, pv_forecast_minute_step, pv_forecast_minute10_step)
            else:
                self.optimise_charge_windows(end_record, load_minutes_step, pv_forecast_minute_step, pv_forecast_minute10_step)
                self.optimise_discharge_windows(end_record, load_minutes_step, pv_forecast_minute_step, pv_forecast_minute10_step)

            # Remove charge windows that overlap with discharge windows
            self.charge_limit_best, self.charge_window_best = self.remove_intersecting_windows(self.charge_limit_best, self.charge_window_best, self.discharge_limits_best, self.discharge_window_best)

            # Filter out any unused discharge windows
            if self.calculate_best_discharge and self.discharge_window_best:
                # Filter out the windows we disabled
                self.discharge_limits_best, self.discharge_window_best = self.discard_unused_discharge_slots(self.discharge_limits_best, self.discharge_window_best)

                # Clipping windows
                if self.discharge_window_best:
                    # Re-run prediction to get data for clipping
                    best_metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle  = self.run_prediction(self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, load_minutes_step, pv_forecast_minute_step, end_record=end_record)

                    # Work out record windows
                    record_discharge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.discharge_window_best), 1)

                    # Discharge slot clipping
                    self.discharge_window_best, self.discharge_limits_best = self.clip_discharge_slots(self.minutes_now, self.predict_soc, self.discharge_window_best, self.discharge_limits_best, record_discharge_windows, PREDICT_STEP) 

                    # Filter out the windows we disabled during clipping
                    self.discharge_limits_best, self.discharge_window_best = self.discard_unused_discharge_slots(self.discharge_limits_best, self.discharge_window_best)
                self.log("Discharge windows filtered {}".format(self.window_as_text(self.discharge_window_best, self.discharge_limits_best)))
            
            # Filter out any unused charge slots
            if self.calculate_best_charge and self.charge_window_best:
                # Re-run prediction to get data for clipping
                best_metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle  = self.run_prediction(self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, load_minutes_step, pv_forecast_minute_step, end_record=end_record)

                # Charge slot clipping
                record_charge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.charge_window_best), 1)
                self.charge_window_best, self.charge_limit_best = self.clip_charge_slots(self.minutes_now, self.predict_soc, self.charge_window_best, self.charge_limit_best, record_charge_windows, PREDICT_STEP) 

                # Charge slot filtering
                if self.set_charge_window:
                    self.charge_limit_best, self.charge_window_best = self.discard_unused_charge_slots(self.charge_limit_best, self.charge_window_best, self.reserve)
                    self.log("Filtered charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, self.charge_limit_best), self.reserve))
                else:
                    self.log("Unfiltered charge windows {} reserve {}".format(self.window_as_text(self.charge_window_best, self.charge_limit_best), self.reserve))

            # Final simulation of best, do 10% and normal scenario
            best_metric10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10, battery_cycle10 = self.run_prediction(self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, load_minutes_step, pv_forecast_minute10_step, save='best10', end_record=end_record)
            best_metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle  = self.run_prediction(self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, load_minutes_step, pv_forecast_minute_step, save='best', end_record=end_record)
            self.log("Best charging limit socs {} export {} gives import battery {} house {} export {} metric {} metric10 {}".format
            (self.charge_limit_best, self.discharge_limits_best, self.dp2(import_kwh_battery), self.dp2(import_kwh_house), self.dp2(export_kwh), self.dp2(best_metric), self.dp2(best_metric10)))

            # Publish charge and discharge window best            
            self.charge_limit_percent_best = self.calc_percent_limit(self.charge_limit_best)
            self.publish_charge_limit(self.charge_limit_best, self.charge_window_best, self.charge_limit_percent_best, best=True)
            self.publish_discharge_limit(self.discharge_window_best, self.discharge_limits_best, best=True)

        if self.holiday_days_left > 0:
            status = "Idle (Holiday)"
        else:
            status = "Idle"

        for inverter in self.inverters:
            # Re-programme charge window based on low rates?
            if self.set_charge_window and self.charge_window_best:
                # Find the next best window and save it
                window = self.charge_window_best[0]
                minutes_start = window['start']
                minutes_end = window['end']

                # Combine contigous windows
                for windows in self.charge_window_best:
                    if minutes_end == windows['start']:
                        minutes_end = windows['end']
                        self.log("Combine window with next window {}-{}".format(self.time_abs_str(windows['start']), self.time_abs_str(windows['end'])))

                # Avoid adjust avoid start time forward when it's already started
                if (inverter.charge_start_time_minutes < self.minutes_now) and (self.minutes_now >= minutes_start):
                    self.log("Include original charge start {}, keeping this instead of new start {}".format(self.time_abs_str(inverter.charge_start_time_minutes), self.time_abs_str(minutes_start)))
                    minutes_start = inverter.charge_start_time_minutes

                # Check if end is within 24 hours of now and end is in the future
                if (minutes_end - self.minutes_now) < 24*60 and minutes_end > self.minutes_now:
                    charge_start_time = self.midnight_utc + timedelta(minutes=minutes_start)
                    charge_end_time = self.midnight_utc + timedelta(minutes=minutes_end)
                    self.log("Charge window will be: {} - {} - current soc {}.target {}".format(charge_start_time, charge_end_time, inverter.soc_percent, self.charge_limit_percent_best[0]))

                    # Are we actually charging?
                    if self.minutes_now >= minutes_start and self.minutes_now < minutes_end:
                        inverter.adjust_charge_rate(inverter.battery_rate_max_charge * 60 * 1000)
                        status = "Charging"

                    # Hold charge mode when enabled
                    if self.set_soc_enable and self.set_reserve_enable and self.set_reserve_hold and (status == "Charging") and ((inverter.soc_percent + 1) >= self.charge_limit_percent_best[0]):
                        status = "Hold charging"
                        inverter.disable_charge_window()
                        self.log("Holding current charge level using reserve: {}".format(self.charge_limit_percent_best[0]))
                    elif (self.minutes_now < minutes_end) and (
                        (minutes_start - self.minutes_now) <= self.set_window_minutes or 
                        (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_window_minutes
                        ):
                        # We must re-program if we are about to start a new charge window or the currently configured window is about to start or has started
                        self.log("Configuring charge window now (now {} target set_window_minutes {} charge start time {}".format(self.time_abs_str(self.minutes_now), self.set_window_minutes, self.time_abs_str(minutes_start)))
                        inverter.adjust_charge_window(charge_start_time, charge_end_time)                        
                    else:
                        self.log("Not setting charging window yet as not within the window (now {} target set_window_minutes {} charge start time {}".format(self.time_abs_str(self.minutes_now),self.set_window_minutes, self.time_abs_str(minutes_start)))

                    # Set configured window minutes for the SOC adjustment routine
                    inverter.charge_start_time_minutes = minutes_start
                    inverter.charge_end_time_minutes = minutes_end
                elif (minutes_end >= 24*60) and (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_window_minutes:
                    # No charging require in the next 24 hours
                    self.log("No charge window required, disabling before the start")
                    inverter.disable_charge_window()
                else:
                    self.log("No change to charge window yet, waiting for schedule.")
            elif self.set_charge_window and (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_window_minutes:
                # No charge windows
                self.log("No charge windows found, disabling before the start")
                inverter.disable_charge_window()
            elif self.set_charge_window:
                self.log("No change to charge window yet, waiting for schedule.")

            # Set discharge modes/window?
            resetReserve = False
            setReserve = False
            if self.set_discharge_window and self.discharge_window_best:
                window = self.discharge_window_best[0]
                minutes_start = window['start']
                minutes_end = window['end']

                # Avoid adjust avoid start time forward when it's already started
                if (inverter.discharge_start_time_minutes < self.minutes_now) and (self.minutes_now >= minutes_start):
                    self.log("Include original discharge start {} with our start which is {}".format(self.time_abs_str(inverter.discharge_start_time_minutes), self.time_abs_str(minutes_start)))
                    minutes_start = inverter.discharge_start_time_minutes

                discharge_start_time = self.midnight_utc + timedelta(minutes=minutes_start)
                discharge_end_time = self.midnight_utc + timedelta(minutes=minutes_end)
                discharge_soc = (self.discharge_limits_best[0] * self.soc_max) / 100.0
                self.log("Next discharge window will be: {} - {} at reserve {}".format(discharge_start_time, discharge_end_time, self.discharge_limits_best[0]))
                if (self.minutes_now >= minutes_start) and (self.minutes_now < minutes_end) and (self.discharge_limits_best[0] < 100.0):
                    if not self.set_discharge_freeze_only and ((self.soc_kw - PREDICT_STEP * inverter.battery_rate_max_discharge_scaled) > discharge_soc):
                        self.log("Discharging now - current SOC {} and target {}".format(self.soc_kw, discharge_soc))
                        inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * 60 * 1000)
                        inverter.adjust_force_discharge(True, discharge_start_time, discharge_end_time)
                        if self.set_reserve_enable:
                            inverter.adjust_reserve(self.discharge_limits_best[0])
                            setReserve = True
                        status = "Discharging"
                        if self.set_discharge_freeze:
                            # In discharge freeze mode we disable charging during discharge slots
                            inverter.adjust_charge_rate(0)
                    else:
                        inverter.adjust_inverter_mode(False)
                        if self.set_discharge_freeze:
                            # In discharge freeze mode we disable charging during discharge slots
                            inverter.adjust_charge_rate(0)
                            self.log("Discharge Freeze as discharge is now at/below target - current SOC {} and target {}".format(self.soc_kw, discharge_soc))
                            status = "Freeze discharging"
                        else:
                            status = "Hold discharging"
                            self.log("Discharge Hold (ECO mode) as discharge is now at/below target or freeze only is set - current SOC {} and target {}".format(self.soc_kw, discharge_soc))
                        resetReserve = True
                else:
                    if (self.minutes_now < minutes_end) and ((minutes_start - self.minutes_now) <= self.set_window_minutes) and self.discharge_limits_best[0]:
                        inverter.adjust_force_discharge(False, discharge_start_time, discharge_end_time)
                        resetReserve = True
                    else:
                        self.log("Setting ECO mode as we are not yet within the discharge window - next time is {} - {}".format(self.time_abs_str(minutes_start), self.time_abs_str(minutes_end)))
                        inverter.adjust_inverter_mode(False)
                        resetReserve = True

                    if self.set_discharge_freeze:
                        # In discharge freeze mode we disable charging during discharge slots, so turn it back on otherwise
                        inverter.adjust_charge_rate(inverter.battery_rate_max_charge * 60 * 1000)
            elif self.set_discharge_window:
                self.log("Setting ECO mode as no discharge window planned")
                inverter.adjust_inverter_mode(False)
                resetReserve = True
                if self.set_discharge_freeze:
                    # In discharge freeze mode we disable charging during discharge slots, so turn it back on otherwise
                    inverter.adjust_charge_rate(inverter.battery_rate_max_charge * 60 * 1000)

            # Car charging from battery disable?
            if not self.car_charging_from_battery:
                car_load = self.in_car_slot(self.minutes_now)
                for car_n in range(0, self.num_cars):
                    if car_load[car_n] > 0:
                        if status not in ['Discharging']:
                            inverter.adjust_discharge_rate(0)
                            self.log("Disabling battery discharge while the car {} is charging".format(car_n))
                            if status != 'Idle':
                                status += ", Hold for car"
                            else:
                                status = "Hold for car"
                        break
                else:
                    inverter.adjust_discharge_rate(inverter.battery_rate_max_discharge * 60 * 1000)

            # Set the SOC just before or within the charge window
            if self.set_soc_enable:
                if self.charge_limit_best and (self.minutes_now < inverter.charge_end_time_minutes) and (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_soc_minutes:
                    inverter.adjust_battery_target(self.charge_limit_percent_best[0])
                else:
                    if not self.inverter_hybrid and self.inverter_soc_reset:
                        if self.charge_limit_best and self.minutes_now >= inverter.charge_start_time_minutes and self.minutes_now < inverter.charge_end_time_minutes:
                            self.log("Within the charge window, holding SOC setting {} (now {} target set_soc_minutes {} charge start time {})".format(self.charge_limit_percent_best[0], self.time_abs_str(self.minutes_now), self.set_soc_minutes, self.time_abs_str(inverter.charge_start_time_minutes)))
                        else:
                            self.log("Resetting charging SOC as we are not within the window and inverter_soc_reset is enabled (now {} target set_soc_minutes {} charge start time {})".format(self.time_abs_str(self.minutes_now), self.set_soc_minutes, self.time_abs_str(inverter.charge_start_time_minutes)))
                            inverter.adjust_battery_target(100.0)
                    else:
                        self.log("Not setting charging SOC as we are not within the window (now {} target set_soc_minutes {} charge start time {})".format(self.time_abs_str(self.minutes_now), self.set_soc_minutes, self.time_abs_str(inverter.charge_start_time_minutes)))

            # If we should set reserve?
            if self.set_soc_enable and self.set_reserve_enable and not setReserve:
                # In the window then set it, otherwise put it back
                if self.charge_limit_best and (self.minutes_now < inverter.charge_end_time_minutes) and (self.minutes_now >= inverter.charge_start_time_minutes):
                    self.log("Adjust reserve to target charge % (set_reserve_enable is true)".format(self.charge_limit_percent_best[0]))
                    inverter.adjust_reserve(self.charge_limit_percent_best[0])
                    resetReserve = False
                else:
                    self.log("Adjust reserve to default (as set_reserve_enable is true)")
                    inverter.adjust_reserve(0)
                    resetReserve = False
            
            # Reset reserve as discharge is enable but not running right now
            if self.set_reserve_enable and resetReserve and not setReserve:
                inverter.adjust_reserve(0)

        # IBoost model update state, only on 5 minute intervals
        if self.iboost_enable and scheduled:
            if self.iboost_energy_today:
                # If we have a realtime sensor just use that data
                self.iboost_next = self.iboost_today
            elif self.minutes_now >= (23*60 + 30):
                # Reset after 11:30pm
                self.iboost_next = 0
            # Save next IBoost model value
            self.expose_config('iboost_today', self.iboost_next)
            self.log("IBoost model today updated to {}".format(self.iboost_next))

        # Holiday days left countdown, subtract a day at midnight every day
        if scheduled and self.holiday_days_left > 0:
            if self.minutes_now < self.get_arg('run_every', 5):
                self.holiday_days_left -= 1
                self.expose_config('holiday_days_left', self.holiday_days_left)
                self.log("Holiday days left is now {}".format(self.holiday_days_left))

        if self.had_errors:
            self.log("Completed run status {} with Errors reported (check log)".format(status))
        else:
            self.log("Completed run status {}".format(status))
            self.record_status(status, debug="best_charge_limit={} best_charge_window={} best_discharge_limit= {} best_discharge_window={}".format(self.charge_limit_best, self.charge_window_best, self.discharge_limits_best, self.discharge_window_best))

    def select_event(self, event, data, kwargs):
        """
        Catch HA Input select updates
        """
        service_data = data.get('service_data', {})
        value = service_data.get('option', None)
        entities = service_data.get('entity_id', [])

        # Can be a string or an array        
        if isinstance(entities, str):
            entities = [entities]

        for item in CONFIG_ITEMS:
            if ('entity' in item) and (item['entity'] in entities):
                entity = item['entity']
                self.log("select_event: {} = {}".format(entity, value))
                self.expose_config(item['name'], value)
                self.update_pending = True
                return

    def number_event(self, event, data, kwargs):
        """
        Catch HA Input number updates
        """
        service_data = data.get('service_data', {})
        value = service_data.get('value', None)
        entities = service_data.get('entity_id', [])

        # Can be a string or an array        
        if isinstance(entities, str):
            entities = [entities]

        for item in CONFIG_ITEMS:
            if ('entity' in item) and (item['entity'] in entities):
                entity = item['entity']
                self.log("number_event: {} = {}".format(entity, value))
                self.expose_config(item['name'], value)
                self.update_pending = True
                return

    def switch_event(self, event, data, kwargs):
        """
        Catch HA Switch toggle
        """
        service = data.get('service', None)
        service_data = data.get('service_data', {})
        entities = service_data.get('entity_id', [])

        # Can be a string or an array        
        if isinstance(entities, str):
            entities = [entities]

        for item in CONFIG_ITEMS:
            if ('entity' in item) and (item['entity'] in entities):
                value = item['value']
                entity = item['entity']

                if service == 'turn_on':
                    value = True
                elif service == 'turn_off':
                    value = False
                elif service == 'toggle' and isinstance(value, bool):
                    value = not value
                
                self.log("switch_event: {} = {}".format(entity, value))
                self.expose_config(item['name'], value)
                self.update_pending = True
                return

    def get_ha_config(self, name):
        """
        Get Home assistant config
        """
        for item in CONFIG_ITEMS:
            if item['name'] == name:
                value = item.get('value')
                return value
        return None

    def expose_config(self, name, value):
        """
        Share the config with HA
        """
        for item in CONFIG_ITEMS:
            if item['name'] == name:
                entity = item.get('entity')
                if entity and ((item.get('value') is None) or (value != item['value'])):
                    item['value'] = value
                    self.log("Updating HA config {} to {}".format(name, value))
                    if item['type'] == 'input_number':
                        icon = item.get('icon', 'mdi:numeric')
                        self.set_state(entity_id = entity, state = value, attributes={'friendly_name' : item['friendly_name'], 'min' : item['min'], 'max' : item['max'], 'step' : item['step'], 'icon' : icon})
                    elif item['type'] == 'switch':
                        icon = item.get('icon', 'mdi:light-switch')
                        self.set_state(entity_id = entity, state = ('on' if value else 'off'), attributes = {'friendly_name' : item['friendly_name'], 'icon' : icon})
                    elif item['type'] == 'select':
                        icon = item.get('icon', 'mdi:format-list-bulleted')
                        self.set_state(entity_id = entity, state = value, attributes = {'friendly_name' : item['friendly_name'], 'options' : item['options'], 'icon' : icon})
                    elif item['type'] == 'update':
                        summary = self.releases.get('this_body', '')
                        latest = self.releases.get('latest', 'check HACS')
                        self.set_state(entity_id = entity, state = 'off', attributes = {'friendly_name' : item['friendly_name'], 'title' : item['title'], 'in_progress' : False, 'auto_update' : False, 
                                                                                        'installed_version' : item['installed_version'], 'latest_version' : latest, 'entity_picture' : item['entity_picture'], 
                                                                                        'release_url' : item['release_url'], 'skipped_version' : False, 'release_summary' : summary})

    def load_user_config(self):
        """
        Load config from HA
        """

        # Find values and monitor config
        for item in CONFIG_ITEMS:
            name = item['name']
            type = item['type']
            entity = type + "." + self.prefix + "_" + name
            item['entity'] = entity
            ha_value = None

            # Get from current state?
            if not self.args.get('user_config_reset', False):
                ha_value = self.get_state(entity)

                # Get from history?
                if ha_value is None:
                    history = self.get_history(entity_id = entity)
                    if history:
                        history = history[0]
                        ha_value = history[-1]['state']

            # Switch convert to text
            if type == 'switch' and isinstance(ha_value, str):
                if ha_value.lower() in ['on', 'true', 'enable']:
                    ha_value = True
                else:
                    ha_value = False

            if type == 'input_number' and ha_value is not None:
                try:
                    ha_value = float(ha_value)
                except (ValueError, TypeError):
                    ha_value = None

            if type == 'update':
                ha_value = None

            # Push back into current state
            if ha_value is not None:
                self.expose_config(item['name'], ha_value)
                
        # Register HA services
        self.fire_event('service_registered', domain="input_number", service="set_value")
        self.fire_event('service_registered', domain="input_number", service="increment")
        self.fire_event('service_registered', domain="input_number", service="decrement")
        self.fire_event('service_registered', domain="switch", service="turn_on")
        self.fire_event('service_registered', domain="switch", service="turn_off")
        self.fire_event('service_registered', domain="switch", service="toggle")        
        self.fire_event('service_registered', domain="select", service="select_option")
        self.fire_event('service_registered', domain="select", service="select_first")
        self.fire_event('service_registered', domain="select", service="select_last")
        self.fire_event('service_registered', domain="select", service="select_next")
        self.fire_event('service_registered', domain="select", service="select_previous")
        self.listen_select_handle = self.listen_event(self.switch_event, event='call_service', domain="switch", service='turn_on')
        self.listen_select_handle = self.listen_event(self.switch_event, event='call_service', domain="switch", service='turn_off')
        self.listen_select_handle = self.listen_event(self.switch_event, event='call_service', domain="switch", service='toggle')
        self.listen_select_handle = self.listen_event(self.number_event, event='call_service', domain="input_number", service='set_value')
        self.listen_select_handle = self.listen_event(self.number_event, event='call_service', domain="input_number", service='increment')
        self.listen_select_handle = self.listen_event(self.number_event, event='call_service', domain="input_number", service='decrement')
        self.listen_select_handle = self.listen_event(self.select_event, event='call_service', domain="select", service='select_option')
        self.listen_select_handle = self.listen_event(self.select_event, event='call_service', domain="select", service='select_first')
        self.listen_select_handle = self.listen_event(self.select_event, event='call_service', domain="select", service='select_last')
        self.listen_select_handle = self.listen_event(self.select_event, event='call_service', domain="select", service='select_next')
        self.listen_select_handle = self.listen_event(self.select_event, event='call_service', domain="select", service='select_previous')

    def resolve_arg_re(self, arg, arg_value, state_keys):
        """
        Resolve argument regular expression on list or string
        """
        matched = True

        if isinstance(arg_value, list):
            new_list = []
            for item_value in arg_value:
                item_matched, item_value = self.resolve_arg_re(arg, item_value, state_keys)
                if not item_matched:
                    self.log('WARN: Regular argument {} expression {} failed to match - disabling this item'.format(arg, item_value))
                    new_list.append(None)
                else:
                    new_list.append(item_value)
            arg_value = new_list
        elif isinstance(arg_value, str) and arg_value.startswith('re:'):
            matched = False
            my_re = '^' + arg_value[3:] + '$'
            for key in state_keys:
                res = re.search(my_re, key)
                if res:
                    if len(res.groups()) > 0:
                        self.log('Regular expression argument {} matched {} with {}'.format(arg, my_re, res.group(1)))
                        arg_value = res.group(1)
                        matched = True
                        break
                    else:
                        self.log('Regular expression argument {} Matched {} with {}'.format(arg, my_re, res.group(0)))
                        arg_value = res.group(0)
                        matched = True
                        break
        return matched, arg_value

    def auto_config(self):
        """
        Auto configure
        match arguments with sensors
        """

        states = self.get_state()
        state_keys = states.keys()
        disabled = []

        if 0:
            predbat_keys = []
            for key in state_keys:
                if 'predbat' in str(key):
                    predbat_keys.append(key)
            predbat_keys.sort()
            self.log("Keys:\n  - entity: {}".format('\n  - entity: '.join(predbat_keys)))

        # Find each arg re to match
        for arg in self.args:
            arg_value = self.args[arg]
            matched, arg_value = self.resolve_arg_re(arg, arg_value, state_keys)
            if not matched:
                self.log("WARN: Regular expression argument: {} unable to match {}, now will disable".format(arg, arg_value))
                disabled.append(arg)
            else:
                self.args[arg] = arg_value

        # Remove unmatched keys
        for key in disabled:
            del self.args[key]

    def state_change(self, entity, attribute, old, new, kwargs):
        """
        State change monitor
        """
        self.log("State change: {} to {}".format(entity, new))

    def initialize(self):
        """
        Setup the app, called once each time the app starts
        """
        global SIMULATE
        self.log("Predbat: Startup")
        try:
            self.reset()
            self.auto_config()
            self.load_user_config()
        except Exception as e:
            self.log("ERROR: Exception raised {}".format(e))
            self.record_status('ERROR: Exception raised {}'.format(e))
            raise e

        # Catch template configurations and exit        
        if self.get_arg('template', False):
            self.log("ERROR: You still have a template configuration, please edit apps.yaml or restart AppDeamon if you just updated with HACS")
            self.record_status("ERROR: You still have a template configuration, please edit apps.yaml or restart AppDeamon if you just updated with HACS")
            return

        if SIMULATE and SIMULATE_LENGTH:
            # run once to get data
            SIMULATE = False
            self.update_pred(scheduled=False)
            soc_best = self.predict_soc_best.copy()
            self.log("Best SOC array {}".format(soc_best))
            SIMULATE = True

            skew = self.get_arg('clock_skew', 0)
            now = datetime.now() + timedelta(minutes=skew)            
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            minutes_now = int((now - midnight).seconds / 60)

            for offset in range (0, SIMULATE_LENGTH, 30):
                self.simulate_offset = offset + 30 - (minutes_now % 30)
                self.sim_soc_kw = soc_best[int(self.simulate_offset / 5) * 5]
                self.log(">>>>>>>>>> Simulated offset {} soc {} <<<<<<<<<<<<".format(self.simulate_offset, self.sim_soc_kw))
                self.update_pred(scheduled=True)
        else:
            # Run every N minutes aligned to the minute
            skew = self.get_arg('clock_skew', 0)
            if skew:
                self.log("WARN: Clock skew is set to {} minutes".format(skew))
            run_every = self.get_arg('run_every', 5) * 60
            skew = skew % (run_every / 60)
            now = datetime.now() + timedelta(minutes=skew)
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            seconds_now = (now - midnight).seconds

            # Calculate next run time to exactly align with the run_every time
            seconds_offset = seconds_now % run_every
            seconds_next = seconds_now + (run_every - seconds_offset)
            next_time = midnight + timedelta(seconds=seconds_next)
            self.log("Predbat: Next run time will be {} and then every {} seconds".format(next_time, run_every))

            # First run is now
            # self.run_in(self.run_time_loop, 0)
            self.update_pending = True

            # Monitor state for inputs
            # self.listen_state(self.state_change, "input_boolean")
            # self.listen_state(self.state_change, "input_select")
            # self.listen_state(self.state_change, "input_number")            

            # And then every N minutes
            if not INVERTER_TEST:
                self.run_every(self.run_time_loop, next_time, run_every, random_start=0, random_end=0)
                self.run_every(self.update_time_loop, datetime.now(), 15, random_start=0, random_end=0)
            else:
                self.update_time_loop(None)

            # Balance inverters
            run_every_balance = self.get_arg('balance_inverters_seconds', 60)
            if run_every_balance > 0:
                self.log("Balance inverters will run every {} seconds (if enabled)".format(run_every_balance))
                seconds_offset_balance = seconds_now % run_every_balance
                seconds_next_balance = seconds_now + (run_every_balance - seconds_offset_balance) + 15 # Offset to start after Predbat update task
                next_time_balance = midnight + timedelta(seconds=seconds_next_balance)
                self.run_every(self.run_time_loop_balance, next_time_balance, run_every_balance, random_start=0, random_end=0)

    def update_time_loop(self, cb_args):
        """
        Called every 15 seconds
        """
        if self.update_pending and not self.prediction_started:
            self.prediction_started = True
            self.update_pending = False
            try:
                self.update_pred(scheduled=False)
            except Exception as e:
                self.log("ERROR: Exception raised {}".format(e))
                self.record_status('ERROR: Exception raised {}'.format(e))
                raise e
            finally:
                self.prediction_started = False
            self.prediction_started = False

    def run_time_loop(self, cb_args):
        """
        Called every N minutes
        """
        if not self.prediction_started:
            self.prediction_started = True
            self.update_pending = False
            try:
                self.update_pred(scheduled=True)
            except Exception as e:
                self.log("ERROR: Exception raised {}".format(e))
                self.record_status('ERROR: Exception raised {}'.format(e))
                raise e
            finally:
                self.prediction_started = False
            self.prediction_started = False

    def run_time_loop_balance(self, cb_args):
        """
        Called every N second for balance inverters
        """
        if not self.prediction_started and self.balance_inverters_enable:
            try:
                self.balance_inverters()
            except Exception as e:
                self.log("ERROR: Exception raised {}".format(e))
                self.record_status('ERROR: Exception raised {}'.format(e))
                raise e
