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

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_OCTOPUS = "%Y-%m-%d %H:%M:%S%z"
PREDICT_STEP = 5

SIMULATE = False         # Debug option, when set don't write to entities but simulate each 30 min period
SIMULATE_LENGTH = 23*60  # How many periods to simulate, set to 0 for just current
INVERTER_TEST = False     # Run inverter control self test

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
    {'name' : 'pv_metric10_weight',            'friendly_name' : 'Metric 10 Weight',               'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'fraction'},
    {'name' : 'pv_scaling',                    'friendly_name' : 'PV Scaling',                     'type' : 'input_number', 'min' : 0,   'max' : 2.0,  'step' : 0.01, 'unit' : 'multiple'},
    {'name' : 'load_scaling',                  'friendly_name' : 'Load Scaling',                   'type' : 'input_number', 'min' : 0,   'max' : 2.0,  'step' : 0.01, 'unit' : 'multiple'},
    {'name' : 'battery_rate_max_scaling',      'friendly_name' : 'Battery rate max scaling',       'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'fraction'},
    {'name' : 'battery_loss',                  'friendly_name' : 'Battery loss charge ',           'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'fraction'},
    {'name' : 'battery_loss_discharge',        'friendly_name' : 'Battery loss discharge',         'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'fraction'},
    {'name' : 'car_charging_energy_scale',     'friendly_name' : 'Car charging energy scale',      'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'fraction'},
    {'name' : 'car_charging_threshold',        'friendly_name' : 'Car charging treshhold',         'type' : 'input_number', 'min' : 4,   'max' : 8.5,  'step' : 0.10, 'unit' : 'kw'},
    {'name' : 'car_charging_rate',             'friendly_name' : 'Car charging rate',              'type' : 'input_number', 'min' : 1,   'max' : 8.5,  'step' : 0.10, 'unit' : 'kw'},
    {'name' : 'car_charging_loss',             'friendly_name' : 'Car charging rate',              'type' : 'input_number', 'min' : 0,   'max' : 1.0,  'step' : 0.01, 'unit' : 'fraction'},
    {'name' : 'best_soc_margin',               'friendly_name' : 'Best SOC Margin',                'type' : 'input_number', 'min' : 0,   'max' : 30.0, 'step' : 0.10, 'unit' : 'kwh'},
    {'name' : 'best_soc_min',                  'friendly_name' : 'Best SOC Min',                   'type' : 'input_number', 'min' : 0,   'max' : 30.0, 'step' : 0.10, 'unit' : 'kwh'},
    {'name' : 'best_soc_keep',                 'friendly_name' : 'Best SOC Keep',                  'type' : 'input_number', 'min' : 0,   'max' : 30.0, 'step' : 0.10, 'unit' : 'kwh'},
    {'name' : 'best_soc_step',                 'friendly_name' : 'Best SOC Step',                  'type' : 'input_number', 'min' : 0.1, 'max' : 1.0,  'step' : 0.05, 'unit' : 'kwh'},
    {'name' : 'metric_min_improvement',        'friendly_name' : 'Metric Min Improvement',         'type' : 'input_number', 'min' : -50, 'max' : 50.0, 'step' : 0.1,  'unit' : 'p'},
    {'name' : 'metric_min_improvement_discharge', 'friendly_name' : 'Metric Min Improvement Discharge',    'type' : 'input_number', 'min' : -50, 'max' : 50.0, 'step' : 0.1,  'unit' : 'p'},
    {'name' : 'set_window_minutes',            'friendly_name' : 'Set Window Minutes',             'type' : 'input_number', 'min' : 5,   'max' : 720,  'step' : 5,    'unit' : 'minutes'},
    {'name' : 'set_soc_minutes',               'friendly_name' : 'Set SOC Minutes',                'type' : 'input_number', 'min' : 5,   'max' : 720,  'step' : 5,    'unit' : 'minutes'},
    {'name' : 'set_reserve_min',               'friendly_name' : 'Set Reserve Min',                'type' : 'input_number', 'min' : 4,   'max' : 100,  'step' : 1,    'unit' : 'percent'},
    {'name' : 'rate_low_threshold',            'friendly_name' : 'Rate Low Treshold',              'type' : 'input_number', 'min' : 0.05,'max' : 0.95, 'step' : 0.05, 'unit' : 'fraction'},
    {'name' : 'rate_high_threshold',           'friendly_name' : 'Rate High Treshold',             'type' : 'input_number', 'min' : 1.0, 'max' : 3.00, 'step' : 0.05, 'unit' : 'fraction'},    
    {'name' : 'car_charging_hold',             'friendly_name' : 'Car charging hold',              'type' : 'switch'},
    {'name' : 'octopus_intelligent_charging',  'friendly_name' : 'Octopus Intelligent Charging',   'type' : 'switch'},
    {'name' : 'car_charging_plan_smart',       'friendly_name' : 'Car Charging Plan Smart',        'type' : 'switch'},
    {'name' : 'calculate_best',                'friendly_name' : 'Calculate Best',                 'type' : 'switch'},
    {'name' : 'calculate_best_charge',         'friendly_name' : 'Calculate Best Charge',          'type' : 'switch'},
    {'name' : 'calculate_charge_oldest',       'friendly_name' : 'Calculate Charge Oldest',        'type' : 'switch'},
    {'name' : 'calculate_charge_all',          'friendly_name' : 'Calculate Charge All',           'type' : 'switch'},
    {'name' : 'calculate_charge_passes',       'friendly_name' : 'Calculate Charge Passes',        'type' : 'input_number', 'min' : 1, 'max' : 2, 'step' : 1, 'unit' : 'number'},    
    {'name' : 'calculate_best_discharge',      'friendly_name' : 'Calculate Best Disharge',        'type' : 'switch'},
    {'name' : 'calculate_discharge_oldest',    'friendly_name' : 'Calculate Discharge Oldest',     'type' : 'switch'},
    {'name' : 'calculate_discharge_all',       'friendly_name' : 'Calculate Discharge All',        'type' : 'switch'},
    {'name' : 'calculate_discharge_first',     'friendly_name' : 'Calculate Discharge First',      'type' : 'switch'},
    {'name' : 'calculate_discharge_passes',    'friendly_name' : 'Calculate Discharge Passes',     'type' : 'input_number', 'min' : 1, 'max' : 2, 'step' : 1, 'unit' : 'number'},    
    {'name' : 'combine_charge_slots',          'friendly_name' : 'Combine Charge Slots',           'type' : 'switch'},
    {'name' : 'combine_discharge_slots',       'friendly_name' : 'Combine Discharge Slots',        'type' : 'switch'},
    {'name' : 'combine_mixed_rates',           'friendly_name' : 'Combined Mixed Rates',           'type' : 'switch'},
    {'name' : 'set_charge_window',             'friendly_name' : 'Set Charge Window',              'type' : 'switch'},
    {'name' : 'set_window_notify',             'friendly_name' : 'Set Window Notify',              'type' : 'switch'},
    {'name' : 'set_discharge_window',          'friendly_name' : 'Set Discharge Window',           'type' : 'switch'},
    {'name' : 'set_discharge_notify',          'friendly_name' : 'Set Discharge Notify',           'type' : 'switch'},
    {'name' : 'set_soc_enable',                'friendly_name' : 'Set Charge Enable',              'type' : 'switch'},
    {'name' : 'set_soc_notify',                'friendly_name' : 'Set Charge Notify',              'type' : 'switch'},
    {'name' : 'set_reserve_enable',            'friendly_name' : 'Set Reserve Enable',             'type' : 'switch'},
    {'name' : 'set_reserve_notify',            'friendly_name' : 'Set Reserve Notify',             'type' : 'switch'},
    {'name' : 'debug_enable',                  'friendly_name' : 'Debug Enable',                   'type' : 'switch'},
    {'name' : 'charge_slot_split',             'friendly_name' : 'Charge Slot Split',              'type' : 'input_number', 'min' : 5,   'max' : 60,  'step' : 5,    'unit' : 'minutes'},
    {'name' : 'discharge_slot_split',          'friendly_name' : 'Discharge Slot Split',           'type' : 'input_number', 'min' : 5,   'max' : 60,  'step' : 5,    'unit' : 'minutes'},
    {'name' : 'car_charging_plan_time',        'friendly_name' : 'Car charging planned ready time','type' : 'select', 'options' : OPTIONS_TIME},
    {'name' : 'rate_low_match_export',         'friendly_name' : 'Rate Low Match Export',          'type' : 'switch'},
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
        self.rest_data = None

        # Rest API?
        self.rest_api = self.base.get_arg('givtcp_rest', None, indirect=False, index=self.id)
        if self.rest_api:
            self.base.log("Inverter {} using Rest API {}".format(self.id, self.rest_api))
            self.rest_data = self.rest_readData()

        # Battery size, charge and discharge rates
        if self.rest_data:
            self.nominal_capacity = float(self.rest_data['raw']['invertor']['battery_nominal_capacity']) / 19.53125  # XXX: Where does 19.53125 come from? I back calculated but why that number...
            self.soc_max = float(self.rest_data['Invertor_Details']['Battery_Capacity_kWh'])
            if abs(self.soc_max - self.nominal_capacity) > 1.0:
                # XXX: Weird workaround for battery reporting wrong capacity issue
                self.base.log("WARN: REST data reports Battery Capacity Kwh as {} but nominal indicates {} - using nominal".format(self.soc_max, self.nominal_capacity))
                self.soc_max = self.nominal_capacity
            self.soc_max *= self.base.battery_scaling
            self.battery_rate_max = self.rest_data['Invertor_Details']['Invertor_Max_Rate'] / 1000.0 / 60.0
        else:
            self.soc_max = self.base.get_arg('soc_max', default=10.0, index=self.id) * self.base.battery_scaling
            self.nominal_capacity = self.soc_max
            self.battery_rate_max = self.base.get_arg('charge_rate', attribute='max', index=self.id, default=2600.0) / 1000.0 / 60.0

        # Battery rate max scaling
        self.battery_rate_max *= self.base.battery_rate_max_scaling

        # Get the current reserve setting or consider the minimum if we are overriding it
        if self.base.set_reserve_enable:
            self.reserve_percent = max(self.base.get_arg('set_reserve_min', 4.0), 4.0)
            self.base.log("Inverter {} Set reserve is enabled, using min reserve {}".format(self.id, self.reserve_percent))
        else:
            if self.rest_data:
                self.reserve_percent = float(self.rest_data['Control']['Battery_Power_Reserve'])
            else:
                self.reserve_percent = max(self.base.get_arg('reserve', default=0.0, index=self.id), 4.0)
            self.base.log("Inverter {} Set reserve is disable, using current reserve {}".format(self.id, self.reserve_percent))
        self.reserve = self.base.dp2(self.soc_max * self.reserve_percent / 100.0)

        # Max inverter rate
        self.inverter_limit = self.base.get_arg('inverter_limit', 7500.0, index=self.id) / (1000 * 60.0)

        self.base.log("New Inverter {} with soc_max {} nominal_capacity {} battery rate kw {} ac limit {} reserve {} %".format(self.id, self.base.dp2(self.soc_max), self.base.dp2(self.nominal_capacity), self.base.dp2(self.battery_rate_max * 60.0), self.base.dp2(self.inverter_limit*60), self.reserve_percent))
        
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

        if SIMULATE:
            self.soc_kw = self.base.sim_soc_kw
        else:
            if self.rest_data:
                self.soc_kw = self.rest_data['Power']['Power']['SOC_kWh'] * self.base.battery_scaling
            else:
                self.soc_kw = self.base.get_arg('soc_kw', default=0.0, index=self.id) * self.base.battery_scaling

        self.base.log("Inverter {} SOC_Kwh {} charge rate {} kw discharge rate kw {}".format(self.id, self.soc_kw, self.charge_rate_max*60*1000, self.discharge_rate_max*60*1000.0))

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
                self.base.record_status("Inverter {} set reserve to {} at {}".format(self.id, reserve, self.base.time_now_str()))
        else:
            self.base.log("Inverter {} Current reserve is {} already at target".format(self.id, current_reserve))

    def adjust_charge_rate(self, new_rate):
        """
        Adjust charging rate
        """
        new_rate = int(new_rate)

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
            self.base.record_status("Inverter {} charge rate changed to {} at {}".format(self.id, new_rate, self.base.time_now_str()))

    def adjust_discharge_rate(self, new_rate):
        """
        Adjust discharging rate
        """
        new_rate = int(new_rate)

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
            self.base.record_status("Inverter {} discharge rate changed to {} at {}".format(self.id, new_rate, self.base.time_now_str()))

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
            self.base.record_status("Inverter {} set soc to {} at {}".format(self.id, soc, self.base.time_now_str()))
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
        return False

    def adjust_force_discharge(self, force_discharge, new_start_time=None, new_end_time=None):
        """
        Adjust force discharge on/off
        """
        if SIMULATE:
            old_inverter_mode = self.base.sim_inverter_mode
            old_start = self.base.sim_discharge_start
            old_end = self.base.sim_discharge_end
        else:
            if self.rest_data:
                old_inverter_mode = self.rest_data['Control']['Mode']
                old_start = self.rest_data['Timeslots']['Discharge_start_time_slot_1']
                old_end = self.rest_data['Timeslots']['Discharge_end_time_slot_1']
            else:
                old_inverter_mode = self.base.get_arg('inverter_mode', index=self.id)
                old_start = self.base.get_arg('discharge_start_time', index=self.id)
                old_end = self.base.get_arg('discharge_end_time', index=self.id)

        # For the purpose of this function consider Eco Paused as the same as Eco (it's a difference in reserve setting)
        if old_inverter_mode == 'Eco (Paused)':
            old_inverter_mode = 'Eco'

        # Force discharge or Eco mode?
        if force_discharge:
            new_inverter_mode = 'Timed Export'
        else:
            new_inverter_mode = 'Eco'

        # Start time to correct format
        if new_start_time:
            new_start = new_start_time.strftime("%H:%M:%S")
        else:
            new_start = None

        # End time to correct format
        if new_end_time:
            new_end = new_end_time.strftime("%H:%M:%S")
        else:
            new_end = None

        self.base.log("Inverter {} Adjust force discharge to {} times {} - {}, current mode {} times {} - {}".format(self.id, new_inverter_mode, new_start, new_end, old_inverter_mode, old_start, old_end))
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

        # Notify
        if changed_start_end:
            self.base.record_status("Inverter {} set discharge slot to {} - {} at {}".format(self.id, new_start, new_end, self.base.time_now_str()))
            if self.base.set_discharge_notify:
                self.base.call_notify("Predbat: Inverter {} Discharge time slot set to {} - {} at time {}".format(self.id, new_start, new_end, self.base.time_now_str()))

        # Change inverter mode
        if old_inverter_mode != new_inverter_mode:
            if SIMULATE:
                self.base.sim_inverter_mode = new_inverter_mode
            else:
                # Inverter mode
                if changed_start_end and not self.rest_api:
                    # XXX: Workaround for GivTCP window state update time to take effort
                    self.base.log("Sleeping (workaround) as start/end of discharge window was just adjusted")
                    time.sleep(30)

                if self.rest_api:
                    self.rest_setBatteryMode(new_inverter_mode)
                else:
                    entity = self.base.get_entity(self.base.get_arg('inverter_mode', indirect=False, index=self.id))
                    self.write_and_poll_option('inverter_mode', entity, new_inverter_mode)

                # Notify
                if self.base.set_discharge_notify:
                    self.base.call_notify("Predbat: Inverter {} Force discharge set to {} at time {}".format(self.id, force_discharge, self.base.time_now_str()))

            self.base.record_status("Inverter {} Set discharge mode to {} at {}".format(self.id, new_inverter_mode, self.base.time_now_str()))
            self.base.log("Inverter {} set force discharge to {}".format(self.id, force_discharge))

    def disable_charge_window(self):
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
                if self.base.set_soc_notify:
                    self.base.call_notify("Predbat: Inverter {} Disabled scheduled charging at {}".format(self.id, self.base.time_now_str()))
            else:
                self.base.sim_charge_schedule_enable = 'off'

            self.base.record_status("Inverter {} Turned off scheduled charge at {}".format(self.id, self.base.time_now_str()))
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

        new_start = charge_start_time.strftime("%H:%M:%S")
        new_end = charge_end_time.strftime("%H:%M:%S")

        self.base.log("Inverter {} charge window is {} - {}, being changed to {} - {}".format(self.id, old_start, old_end, new_start, new_end))

        if old_charge_schedule_enable == 'off' or old_charge_schedule_enable == 'disable':
            if not SIMULATE:
                # Enable scheduled charge if not turned on
                if self.rest_api:
                    self.rest_enableChargeSchedule(True)
                else:
                    entity = self.base.get_entity(self.base.get_arg('scheduled_charge_enable', indirect=False, index=self.id))
                    self.write_and_poll_switch('scheduled_charge_enable', entity, True)
                if self.base.set_soc_notify:
                    self.base.call_notify("Predbat: Inverter {} Enabling scheduled charging at {}".format(self.id, self.base.time_now_str()))
            else:
                self.base.sim_charge_schedule_enable = 'on'

            self.charge_enable_time = True
            self.base.record_status("Inverter {} Turned on charge enable".format(self.id))
            self.base.log("Inverter {} Turning on scheduled charge".format(self.id))

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
            self.base.record_status("Inverter {} Charge window change to: {} - {} at {}".format(self.id, new_start, new_end, self.base.time_now_str()))
            self.base.log("Inverter {} Updated start and end charge window to {} - {} (old {} - {})".format(self.id, new_start, new_end, old_start, old_end))

    def rest_readData(self):
        """
        Get inverter status
        """
        url = self.rest_api + '/readData'
        r = requests.get(url)
        if r.status_code == 200:
            return r.json()
        else:
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
            new = self.rest_data['Control']['Battery_Discharge_Rate']
            if abs(new - rate) <  100:
                self.base.log("Inverter {} set discharge rate {} via REST succesfull on retry {}".format(self.id, rate, retry))
                return True

        self.base.log("WARN: Inverter {} set discharge rate {} via REST failed got {}".format(self.id, rate, self.rest_data['Control']['Battery_Discharge_Rate']))
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
                    except ValueError:
                        self.log("WARN: Return bad value {} from {} arg {}".format(got, item, arg))
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
                value = value.format(**self.args)

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
        if self.args.get('user_config_enable', False):
            value = self.get_ha_config(arg)

        # Resolve locally if no HA config
        if value is None:
            value = self.args.get(arg, default)
            value = self.resolve_arg(arg, value, default=default, indirect=indirect, combine=combine, attribute=attribute, index=index)

        if isinstance(default, float):
            # Convert to float?
            try:
                value = float(value)
            except ValueError:
                self.log("WARN: Return bad float value {} from {} using default {}".format(value, arg, default))
                value = default
        elif isinstance(default, int) and not isinstance(default, bool):
            # Convert to int?
            try:
                value = int(value)
            except ValueError:
                self.log("WARN: Return bad int value {} from {} using default {}".format(value, arg, default))
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
        if self.args.get('user_config_enable', False):
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
            return False
        if not gekey:
            self.log("ERROR: GE Cloud has been enabled but ge_cloud_key is not set to your appkey")
            return False

        headers = {
            'Authorization': 'Bearer  ' + gekey,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        mdata = []
        days_prev = 0
        while days_prev <= self.max_days_previous:
            time_value = now_utc - timedelta(days=days_prev)
            datestr = time_value.strftime("%Y-%m-%d")
            url = "https://api.givenergy.cloud/v1/inverter/{}/data-points/{}?pageSize=1024".format(geserial, datestr)
            while url:
                data = self.get_ge_url(url, headers, now_utc)

                darray = data.get('data', None)
                if darray is None:
                    self.log("WARN: Error downloading GE data from url {}".format(url))
                    return False

                for item in darray:
                    timestamp = item['time']
                    consumption = item['today']['consumption']
                    dimport = item['today']['grid']['import']
                    dexport = item['today']['grid']['export']

                    new_data = {}
                    new_data['last_updated'] = timestamp
                    new_data['consumption'] = consumption
                    new_data['import'] = dimport
                    new_data['export'] = dexport
                    mdata.append(new_data)
                url = data['links'].get('next', None)
            days_prev += 1
            
        self.load_minutes = self.minute_data(mdata, self.max_days_previous, now_utc, 'consumption', 'last_updated', backwards=True, smoothing=True, scale=self.load_scaling, clean_increment=True)
        self.import_today = self.minute_data(mdata, self.max_days_previous, now_utc, 'import', 'last_updated', backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True)
        self.export_today = self.minute_data(mdata, self.max_days_previous, now_utc, 'export', 'last_updated', backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True)
        self.log("Downloaded {} datapoints from GE".format(len(self.load_minutes)))
        return True

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
                return {}
            if 'results' in data:
                mdata += data['results']
            else:
                self.log("WARN: Error downloading Octopus data from url {}".format(url))
                return {}
            url = data.get('next', None)
            pages += 1
        pdata = self.minute_data(mdata, self.forecast_days + 1, self.midnight_utc, 'value_inc_vat', 'valid_from', backwards=False, to_key='valid_to')
        return pdata

    def mintes_to_time(self, updated, now):
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
            history = self.get_history(entity_id = entity_id, days = self.max_days_previous)
            if history:
                import_today = self.minute_data(history[0], self.max_days_previous, now_utc, 'state', 'last_updated', backwards=True, smoothing=True, scale=self.import_export_scaling, clean_increment=True, accumulate=import_today)
            else:
                self.log("WARN: Unable to fetch history for {}".format(entity_id))

        return import_today

    def minute_data_load(self, now_utc):
        """
        Download one or more entities for load data
        """
        entity_ids = self.get_arg('load_today', indirect=False)
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        load_minutes = {}
        for entity_id in entity_ids:
            history = self.get_history(entity_id = entity_id, days = self.max_days_previous)
            if history:
                load_minutes = self.minute_data(history[0], self.max_days_previous, now_utc, 'state', 'last_updated', backwards=True, smoothing=True, scale=self.load_scaling, clean_increment=True, accumulate=load_minutes)
            else:
                self.log("WARN: Unable to fetch history for {}".format(entity_id))
        return load_minutes

    def minute_data(self, history, days, now, state_key, last_updated_key,
                    backwards=False, to_key=None, smoothing=False, clean_increment=False, divide_by=0, scale=1.0, accumulate=[]):
        """
        Turns data from HA into a hash of data indexed by minute with the data being the value
        Can be backwards in time for history (N minutes ago) or forward in time (N minutes in the future)
        """
        mdata = {}
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
            except ValueError:
                continue

            # Divide down the state if required
            if divide_by:
                state /= divide_by
            
            # Update prev to the first if not set
            if not prev_last_updated_time:
                prev_last_updated_time = last_updated_time
                last_state = state

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
                        # Reset to zero?
                        if state < last_state and (state == 0.0):
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
            nxt = data[rindex]
            if nxt >= last:
                increment += nxt - last
            last = nxt
            new_data[rindex] = increment

        return new_data

    def get_historical(self, data, minute):
        """
        Get historical data across N previous days in days_previous array based on current minute 
        """
        total = 0
        num_points = 0

        for days in self.days_previous:
            full_days = 24*60*(days - 1)
            minute_previous = 24 * 60 - minute + full_days
            value = self.get_from_incrementing(data, minute_previous)
            total += value
            num_points += 1
        return total / num_points

    def get_from_incrementing(self, data, index):
        """
        Get a single value from an incrementing series e.g. kwh today -> kwh this minute
        """
        while index < 0:
            index += 24*60
        return data[index] - data[index + 1]

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

    def record_status(self, message, debug=""):
        """
        Records status to HA sensor
        """
        self.set_state("predbat.status", state=message, attributes = {'friendly_name' : 'Status', 'icon' : 'mdi:information', 'debug' : debug})

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

    def run_prediction(self, charge_limit, charge_window, discharge_window, discharge_limits, load_minutes, pv_forecast_minute, save=None, step=PREDICT_STEP, end_record=None):
        """
        Run a prediction scenario given a charge limit, options to save the results or not to HA entity
        """
        predict_soc = {}
        predict_battery_power = {}
        predict_soc_time = {}
        predict_car_soc_time = {}
        predict_pv_power = {}
        predict_state = {}
        predict_grid_power = {}
        predict_load_power = {}
        minute = 0
        minute_left = self.forecast_minutes
        soc = self.soc_kw
        soc_min = self.soc_max
        soc_min_minute = self.minutes_now
        charge_has_run = False
        charge_has_started = False
        discharge_has_run = False
        export_kwh = 0
        import_kwh = 0
        import_kwh_house = 0
        import_kwh_battery = 0
        final_export_kwh = 0
        final_import_kwh = 0
        final_import_kwh_house = 0
        final_import_kwh_battery = 0
        load_kwh = 0
        final_load_kwh = 0
        pv_kwh = 0
        final_pv_kwh = 0
        metric = self.cost_today_sofar
        final_metric = metric
        metric_time = {}
        load_kwh_time = {}
        pv_kwh_time = {}
        export_kwh_time = {}
        import_kwh_time = {}
        record_time = {}
        car_soc = self.car_charging_soc
        final_car_soc = car_soc
        charge_rate_max = self.charge_rate_max
        discharge_rate_max = self.discharge_rate_max
        battery_state = "-"
        grid_state = '-'

        # self.log("Sim discharge window {} enable {}".format(discharge_window, discharge_limits))

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
                predict_car_soc_time[stamp] = self.dp2(car_soc / self.car_charging_battery_size * 100.0)
                record_time[stamp] = 0 if record else self.soc_max

            # Save Soc prediction data as minutes for later use
            self.predict_soc[minute] = self.dp3(soc)
            if save and save=='best':
                self.predict_soc_best[minute] = self.dp3(soc)

            # Get load and pv forecast, total up for all values in the step
            pv_now = 0
            load_yesterday = 0
            for offset in range(0, step):
                pv_now += pv_forecast_minute.get(minute_absolute + offset, 0.0)
                load_yesterday += self.get_historical(load_minutes, minute - offset)

            # Count PV kwh
            pv_kwh += pv_now
            if record:
                final_pv_kwh = pv_kwh

            # Car charging hold
            if self.car_charging_hold and self.car_charging_energy:
                # Hold based on data
                car_energy = 0
                for offset in range(0, step):
                    car_energy += self.get_historical(self.car_charging_energy, minute - offset)
                load_yesterday = max(0, load_yesterday - car_energy)
            elif self.car_charging_hold and (load_yesterday >= (self.car_charging_threshold * step)):
                # Car charging hold - ignore car charging in computation based on threshold
                load_yesterday = max(load_yesterday - (self.car_charging_rate * step / 60.0), 0)

            # Simulate car charging
            car_load = 0.0
            if self.car_charging_slots:
                # Octopus slot car charging?
                car_load = self.in_car_slot(minute_absolute)

            # Car charging?
            if car_load > 0.0:
                car_load_scale = car_load * step / 60.0
                car_load_scale = car_load_scale * self.car_charging_loss
                car_load_scale = max(min(car_load_scale, self.car_charging_limit - car_soc), 0)
                car_soc += car_load_scale
                load_yesterday += car_load_scale / self.car_charging_loss

            # Count load
            load_kwh += load_yesterday
            if record:
                final_load_kwh = load_kwh

            pv_ac = min(load_yesterday, pv_now, self.inverter_limit * step)
            pv_dc = pv_now - pv_ac

            # Battery behaviour
            battery_draw = 0
            if (charge_window_n >= 0) and soc < charge_limit[charge_window_n]:
                # Charge enable
                charge_rate_max = self.battery_rate_max  # Assume charge becomes enabled here
                battery_draw = -max(min(charge_rate_max * step, charge_limit[charge_window_n] - soc), 0)
                battery_state = 'f+'
            elif (discharge_window_n >= 0) and discharge_limits[discharge_window_n] < 100.0 and soc >= ((self.soc_max * discharge_limits[discharge_window_n]) / 100.0):
                # Discharge enable
                discharge_rate_max = self.battery_rate_max  # Assume discharge becomes enabled here
                # It's assumed if SOC hits the expected reserve then it's terminated
                reserve_expected = (self.soc_max * discharge_limits[discharge_window_n]) / 100.0
                battery_draw = discharge_rate_max * step
                if (soc - reserve_expected) < battery_draw:
                    battery_draw = max(soc - reserve_expected, 0)
                battery_state = 'f-'
            else:
                # ECO Mode
                if load_yesterday - pv_ac - pv_dc > 0:
                    battery_draw = min(load_yesterday - pv_ac - pv_dc, discharge_rate_max * step, self.inverter_limit * step - pv_ac)
                    battery_state = 'e-'
                else:
                    battery_draw = max(load_yesterday - pv_ac - pv_dc, -charge_rate_max * step)
                    if battery_draw > 0:
                        battery_state = 'e+'
                    else:
                        battery_state = 'e~'

            # Clamp battery at reserve
            if battery_draw > 0:
                soc -= battery_draw / self.battery_loss_discharge
                if soc < self.reserve:
                    battery_draw -= (self.reserve - soc) * self.battery_loss_discharge
                    soc = self.reserve

            # Clamp battery at max
            if battery_draw < 0:
                soc -= battery_draw * self.battery_loss
                if soc > self.soc_max:
                    battery_draw += (soc - self.soc_max) / self.battery_loss
                    soc = self.soc_max

            # Work out left over energy after battery adjustment
            diff = load_yesterday - (battery_draw + pv_dc + pv_ac)
            if diff < 0:
                # Can not export over inverter limit, load must be taken out first from the inverter limit
                inverter_left = self.inverter_limit * step - load_yesterday
                if inverter_left < 0:
                    diff += -inverter_left
                else:
                    diff = max(diff, -inverter_left)

            if diff > 0:
                # Import
                import_kwh += diff
                if charge_window_n >= 0:
                    # If the battery is on charge anyhow then imports are at battery charging rate
                    import_kwh_battery += diff
                else:
                    # self.log("importing to minute %s amount %s kw total %s kwh total draw %s" % (minute, energy, import_kwh_house, diff))
                    import_kwh_house += diff

                if minute_absolute in self.rate_import:
                    metric += self.rate_import[minute_absolute] * diff
                else:
                    if charge_window_n >= 0:
                        metric += self.metric_battery * diff
                    else:
                        metric += self.metric_house * diff
                grid_state = '<'
            else:
                # Export
                energy = -diff
                export_kwh += energy
                if minute_absolute in self.rate_export:
                    metric -= self.rate_export[minute_absolute] * energy
                else:
                    metric -= self.metric_export * energy
                if diff != 0:
                    grid_state = '>'
                else:
                    grid_state = '~'
            
            # Store the number of minutes until the battery runs out
            if record and soc <= self.reserve:
                minute_left = max(minute, minute_left)

            # Record final soc & metric
            if record:
                final_soc = soc
                final_car_soc = car_soc
                final_metric = metric
                final_import_kwh = import_kwh
                final_import_kwh_battery = import_kwh_battery
                final_import_kwh_house = import_kwh_house
                final_export_kwh = export_kwh

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
                predict_battery_power[stamp] = battery_draw * (60 / step)
                predict_pv_power[stamp] = pv_now  * (60 / step)
                predict_grid_power[stamp] = diff * (60 / step)
                predict_load_power[stamp] = load_yesterday * (60 / step)

            minute += step

        hours_left = minute_left / 60.0
        charge_limit_percent = [min(int((float(charge_limit[i]) / self.soc_max * 100.0) + 0.5), 100) for i in range(0, len(charge_limit))]

        if self.debug_enable or save:
            self.log("predict {} end_record {} final soc {} kwh metric {} p min_soc {} @ {} kwh load {} pv {} charge {} limit {}% ({} kwh)".format(
                      save, self.time_abs_str(end_record + self.minutes_now), self.dp2(final_soc), self.dp2(final_metric), self.dp2(soc_min), self.time_abs_str(soc_min_minute), self.dp2(final_load_kwh), self.dp2(final_pv_kwh), charge_window, charge_limit_percent, charge_limit))
            self.log("         [{}]".format(self.scenario_summary_title(record_time)))
            self.log("    SOC: [{}]".format(self.scenario_summary(record_time, predict_soc_time)))
            self.log("  STATE: [{}]".format(self.scenario_summary(record_time, predict_state)))
            self.log("   LOAD: [{}]".format(self.scenario_summary(record_time, load_kwh_time)))
            self.log("     PV: [{}]".format(self.scenario_summary(record_time, pv_kwh_time)))
            self.log(" IMPORT: [{}]".format(self.scenario_summary(record_time, import_kwh_time)))
            self.log(" EXPORT: [{}]".format(self.scenario_summary(record_time, export_kwh_time)))
            self.log("    CAR: [{}]".format(self.scenario_summary(record_time, predict_car_soc_time)))
            self.log(" METRIC: [{}]".format(self.scenario_summary(record_time, metric_time)))

        # Save data to HA state
        if save and save=='base' and not SIMULATE:
            self.set_state("predbat.battery_hours_left", state=self.dp2(hours_left), attributes = {'friendly_name' : 'Predicted Battery Hours left', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'icon' : 'mdi:timelapse'})
            self.set_state("predbat.car_soc", state=self.dp2(final_car_soc / self.car_charging_battery_size * 100.0), attributes = {'results' : predict_car_soc_time, 'friendly_name' : 'Car battery SOC', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' : 'mdi:battery'})
            self.set_state("predbat.soc_kw_h0", state=self.dp3(self.predict_soc[0]), attributes = {'friendly_name' : 'Current SOC kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.soc_kw", state=self.dp3(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Predicted SOC kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.battery_power", state=self.dp3(final_soc), attributes = {'results' : predict_battery_power, 'friendly_name' : 'Predicted Battery Power', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state("predbat.pv_power", state=self.dp3(final_soc), attributes = {'results' : predict_pv_power, 'friendly_name' : 'Predicted PV Power', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state("predbat.grid_power", state=self.dp3(final_soc), attributes = {'results' : predict_grid_power, 'friendly_name' : 'Predicted Grid Power', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state("predbat.load_power", state=self.dp3(final_soc), attributes = {'results' : predict_load_power, 'friendly_name' : 'Predicted Load Power', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state("predbat.soc_min_kwh", state=self.dp3(soc_min), attributes = {'time' : self.time_abs_str(soc_min_minute), 'friendly_name' : 'Predicted minimum SOC best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery-arrow-down-outline'})
            self.publish_charge_limit(charge_limit, charge_window, charge_limit_percent, best=False)
            self.set_state("predbat.export_energy", state=self.dp3(final_export_kwh), attributes = {'results' : export_kwh_time, 'friendly_name' : 'Predicted exports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-export'})
            self.set_state("predbat.load_energy", state=self.dp3(final_load_kwh), attributes = {'results' : load_kwh_time, 'friendly_name' : 'Predicted load', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:home-lightning-bolt'})
            self.set_state("predbat.pv_energy", state=self.dp3(final_pv_kwh), attributes = {'results' : pv_kwh_time, 'friendly_name' : 'Predicted PV', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:solar-power'})
            self.set_state("predbat.import_energy", state=self.dp3(final_import_kwh), attributes = {'results' : import_kwh_time, 'friendly_name' : 'Predicted imports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state("predbat.import_energy_battery", state=self.dp3(final_import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state("predbat.import_energy_house", state=self.dp3(final_import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.log("Battery has {} hours left - now at {}".format(hours_left, self.dp2(self.soc_kw)))
            self.set_state("predbat.metric", state=self.dp2(final_metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            self.set_state("predbat.duration", state=self.dp2(end_record/60), attributes = {'friendly_name' : 'Prediction duration', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'icon' : 'mdi:arrow-split-vertical'})

        if save and save=='best' and not SIMULATE:
            self.set_state("predbat.best_battery_hours_left", state=self.dp2(hours_left), attributes = {'friendly_name' : 'Predicted Battery Hours left best', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'icon' : 'mdi:timelapse'})
            self.set_state("predbat.car_soc_best", state=self.dp2(final_car_soc / self.car_charging_battery_size * 100.0), attributes = {'results' : predict_car_soc_time, 'friendly_name' : 'Car battery SOC best', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' : 'mdi:battery'})
            self.set_state("predbat.soc_kw_best", state=self.dp3(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.battery_power_best", state=self.dp3(final_soc), attributes = {'results' : predict_battery_power, 'friendly_name' : 'Predicted Battery Power Best', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state("predbat.pv_power_best", state=self.dp3(final_soc), attributes = {'results' : predict_pv_power, 'friendly_name' : 'Predicted PV Power Best', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state("predbat.grid_power_best", state=self.dp3(final_soc), attributes = {'results' : predict_grid_power, 'friendly_name' : 'Predicted Grid Power Best', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state("predbat.load_power_best", state=self.dp3(final_soc), attributes = {'results' : predict_load_power, 'friendly_name' : 'Predicted Load Power Best', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state("predbat.soc_kw_best_h1", state=self.dp3(self.predict_soc[60]), attributes = {'friendly_name' : 'Predicted SOC kwh best + 1h', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.soc_kw_best_h8", state=self.dp3(self.predict_soc[60*8]), attributes = {'friendly_name' : 'Predicted SOC kwh best + 8h', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.soc_kw_best_h12", state=self.dp3(self.predict_soc[60*12]), attributes = {'friendly_name' : 'Predicted SOC kwh best + 12h', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.best_soc_min_kwh", state=self.dp3(soc_min), attributes = {'time' : self.time_abs_str(soc_min_minute), 'friendly_name' : 'Predicted minimum SOC best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery-arrow-down-outline'})
            self.set_state("predbat.best_export_energy", state=self.dp3(final_export_kwh), attributes = {'results' : export_kwh_time, 'friendly_name' : 'Predicted exports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-export'})
            self.set_state("predbat.best_load_energy", state=self.dp3(final_load_kwh), attributes = {'results' : load_kwh_time, 'friendly_name' : 'Predicted load best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:home-lightning-bolt'})
            self.set_state("predbat.best_pv_energy", state=self.dp3(final_pv_kwh), attributes = {'results' : pv_kwh_time, 'friendly_name' : 'Predicted PV best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:solar-power'})
            self.set_state("predbat.best_import_energy", state=self.dp3(final_import_kwh), attributes = {'results' : import_kwh_time, 'friendly_name' : 'Predicted imports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state("predbat.best_import_energy_battery", state=self.dp3(final_import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state("predbat.best_import_energy_house", state=self.dp3(final_import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})
            self.set_state("predbat.best_metric", state=self.dp2(final_metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted best metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            self.set_state("predbat.record", state=0.0, attributes = {'results' : record_time, 'friendly_name' : 'Prediction window', 'state_class' : 'measurement'})

        if save and save=='debug' and not SIMULATE:
            self.set_state("predbat.pv_power_debug", state=self.dp3(final_soc), attributes = {'results' : predict_pv_power, 'friendly_name' : 'Predicted PV Power Debug', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state("predbat.grid_power_debug", state=self.dp3(final_soc), attributes = {'results' : predict_grid_power, 'friendly_name' : 'Predicted Grid Power Debug', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state("predbat.load_power_debug", state=self.dp3(final_soc), attributes = {'results' : predict_load_power, 'friendly_name' : 'Predicted Load Power Debug', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
            self.set_state("predbat.battery_power_debug", state=self.dp3(final_soc), attributes = {'results' : predict_battery_power, 'friendly_name' : 'Predicted Battery Power Debug', 'state_class': 'measurement', 'unit_of_measurement': 'kw', 'icon' : 'mdi:battery'})
 
        if save and save=='best10' and not SIMULATE:
            self.set_state("predbat.soc_kw_best10", state=self.dp3(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh best 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:battery'})
            self.set_state("predbat.best10_pv_energy", state=self.dp3(final_pv_kwh), attributes = {'results' : pv_kwh_time, 'friendly_name' : 'Predicted PV best 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:solar-power'})
            self.set_state("predbat.best10_metric", state=self.dp2(final_metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted best 10% metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            self.set_state("predbat.best10_export_energy", state=self.dp3(final_export_kwh), attributes = {'results' : export_kwh_time, 'friendly_name' : 'Predicted exports best 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-export'})
            self.set_state("predbat.best10_load_energy", state=self.dp3(final_load_kwh), attributes = {'friendly_name' : 'Predicted load best 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' : 'mdi:home-lightning-bolt'})
            self.set_state("predbat.best10_import_energy", state=self.dp3(final_import_kwh), attributes = {'results' : import_kwh_time, 'friendly_name' : 'Predicted imports best 10%', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon': 'mdi:transmission-tower-import'})

        return final_metric, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min, final_soc, soc_min_minute

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

    def rate_replicate(self, rates):
        """
        We don't get enough hours of data for Octopus, so lets assume it repeats until told others
        """
        minute = 0
        rate_last = 0
        # Add 12 extra hours to make sure charging period will end
        while minute < (self.forecast_minutes + 24*60):
            if minute not in rates:
                minute_mod = minute % (24*60)
                if minute_mod in rates:
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

    def basic_rates(self, info, rtype):
        """
        Work out the energy rates based on user supplied time periods
        works on a 24-hour period only and then gets replicated later for future days
        """
        rates = {}

        # Default to house value
        for minute in range(0, 24*60):
            rates[minute] = self.metric_house

        self.log("Adding {} rate info {}".format(rtype, info))
        midnight = datetime.strptime('00:00:00', "%H:%M:%S")
        for this_rate in info:
            start = datetime.strptime(this_rate.get('start', "00:00:00"), "%H:%M:%S")
            end = datetime.strptime(this_rate.get('end', "00:00:00"), "%H:%M:%S")
            rate = this_rate.get('rate', self.metric_house)
            start_minutes = max(self.mintes_to_time(start, midnight), 0)
            end_minutes   = min(self.mintes_to_time(end, midnight), 24*60-1)

            if end_minutes <= start_minutes:
                end_minutes += 24*60

            # self.log("Found rate {} {} to {} minutes".format(rate, start_minutes, end_minutes))
            for minute in range(start_minutes, end_minutes):
                rates[minute % (24*60)] = rate

        return rates

    def plan_car_charging(self, low_rates):
        """
        Plan when the car will charge, taking into account ready time and pricing
        """
        plan = []
        car_soc = self.car_charging_soc
        
        if self.car_charging_plan_smart:
            price_sorted = self.sort_window_by_price(low_rates)
        else:
            price_sorted = range(0, len(low_rates))

        ready_time = datetime.strptime(self.car_charging_plan_time, "%H:%M:%S")
        ready_minutes = ready_time.hour * 60 + ready_time.minute
        self.log("Ready time {} minutes {}".format(ready_time, ready_minutes))

        # Ready minutes wrap?
        if ready_minutes < self.minutes_now:
            ready_minutes += 24*60

        for window_n in price_sorted:
            window = low_rates[window_n]
            start = max(window['start'], self.minutes_now)
            end = min(window['end'], ready_minutes)
            length = 0
            kwh = 0

            if car_soc >= self.car_charging_limit:
                break

            if end <= start:
                continue

            length = end - start
            hours = length / 60
            kwh = self.car_charging_rate * hours

            kwh_add = kwh * self.car_charging_loss
            kwh_left = self.car_charging_limit - car_soc

            # Clamp length to required amount (shorten the window)
            if kwh_add > kwh_left:
                percent = kwh_left / kwh_add
                length = int((length * percent) / 5 + 2.5) * 5
                end = start + length
                hours = length / 60
                kwh = self.car_charging_rate * hours
                kwh_add = kwh * self.car_charging_loss

            # Work out how much to add to the battery, include losses
            kwh_add = max(min(kwh_add, self.car_charging_limit - car_soc), 0)
            kwh = kwh_add / self.car_charging_loss

            # Work out charging amounts
            if kwh > 0:
                car_soc += kwh_add
                new_slot = {}
                new_slot['start'] = start
                new_slot['end'] = end
                new_slot['kwh'] = kwh
                plan.append(new_slot)

        # Return sorted back in time order
        return self.sort_window_by_time(plan)

    def load_octopus_slots(self, octopus_slots):
        """
        Turn octopus slots into charging plan
        """
        new_slots = []

        for slot in octopus_slots:
            start = datetime.strptime(slot['startDtUtc'], TIME_FORMAT_OCTOPUS)
            start_minutes = max(self.mintes_to_time(start, self.midnight_utc), 0)
            end = datetime.strptime(slot['endDtUtc'], TIME_FORMAT_OCTOPUS)
            end_minutes   = min(self.mintes_to_time(end, self.midnight_utc), self.forecast_minutes)
            slot_minutes = end_minutes - start_minutes
            slot_hours = slot_minutes / 60.0

            # The load expected is stored in chargeKwh for the period in use
            kwh = abs(float(slot.get('chargeKwh', self.car_charging_rate * slot_hours)))

            if end_minutes > self.minutes_now:
                new_slot = {}
                new_slot['start'] = start_minutes
                new_slot['end'] = end_minutes
                new_slot['kwh'] = kwh
                new_slots.append(new_slot)
        return new_slots

    def in_car_slot(self, minute):
        """
        Is the given minute inside a car slot
        """
        if self.car_charging_slots:
            for slot in self.car_charging_slots:
                start_minutes = slot['start']
                end_minutes = slot['end']
                kwh = slot['kwh']
                slot_minutes = end_minutes - start_minutes
                slot_hours = slot_minutes / 60.0

                # Return the load in that slot
                if minute >= start_minutes and minute < end_minutes:
                    return abs(kwh / slot_hours)
        return 0

    def rate_scan_export(self, rates):
        """
        Scan the rates and work out min/max and charging windows for export
        """
        rate_low_min_window = 5
        rate_high_threshold = self.rate_high_threshold

        rate_min, rate_max, rate_average, rate_min_minute, rate_max_minute = self.rate_minmax(rates)
        self.log("Export rates min {} max {} average {}".format(rate_min, rate_max, rate_average))

        self.rate_export_min = rate_min
        self.rate_export_max = rate_max
        self.rate_export_min_minute = rate_min_minute
        self.rate_export_max_minute = rate_max_minute
        self.rate_export_average = rate_average
        self.rate_export_threshold = rate_average * rate_high_threshold

        # Find discharging windows
        self.high_export_rates = self.rate_scan_window(rates, rate_low_min_window, rate_average * rate_high_threshold, True)
        return rates

    def publish_car_plan(self):
        """
        Publish the car charging plan
        """
        plan = []

        if not self.car_charging_slots:
            self.set_state("binary_sensor.predbat_car_charging_slot", state='off', attributes = {'planned' : plan, 'friendly_name' : 'Predbat car charging slot', 'icon': 'mdi:home-lightning-bolt-outline'})
        else:
            window = self.car_charging_slots[0]
            if self.minutes_now >= window['start'] and self.minutes_now < window['end']:
                slot = True
            else:
                slot = False

            for window in self.car_charging_slots:
                start = self.time_abs_str(window['start'])
                end = self.time_abs_str(window['end'])
                kwh = self.dp2(window['kwh'])
                show = {}
                show['start'] = start
                show['end'] = end
                show['kwh'] = kwh
                plan.append(show)

            self.set_state("binary_sensor.predbat_car_charging_slot", state="on" if slot else 'off', attributes = {'planned' : plan, 'friendly_name' : 'Predbat car charging slot', 'icon': 'mdi:home-lightning-bolt-outline'})

    def publish_rates_export(self):
        if self.high_export_rates:
            window_n = 0
            for window in self.high_export_rates:
                rate_high_start = window['start']
                rate_high_end = window['end']
                rate_high_average = window['average']

                self.log("High export rate window:{} - {} to {} @{} !".format(window_n, self.time_abs_str(rate_high_start), self.time_abs_str(rate_high_end), rate_high_average))

                rate_high_start_date = self.midnight_utc + timedelta(minutes=rate_high_start)
                rate_high_end_date = self.midnight_utc + timedelta(minutes=rate_high_end)

                time_format_time = '%H:%M:%S'

                if window_n == 0 and not SIMULATE:
                    self.set_state("predbat.high_rate_export_start", state=rate_high_start_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next high export rate start', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.high_rate_export_end", state=rate_high_end_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next high export rate end', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.high_rate_export_cost", state=self.dp2(rate_high_average), attributes = {'friendly_name' : 'Next high export rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
                if window_n == 1 and not SIMULATE:
                    self.set_state("predbat.high_rate_export_start_2", state=rate_high_start_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next+1 high export rate start', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.high_rate_export_end_2", state=rate_high_end_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next+1 high export rate end', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.high_rate_export_cost_2", state=self.dp2(rate_high_average), attributes = {'friendly_name' : 'Next+1 high export rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
                window_n += 1

        # Clear rates that aren't available
        if not self.high_export_rates and not SIMULATE:
            self.log("No high rate period found")
            self.set_state("predbat.high_rate_export_start", state='undefined', attributes = {'friendly_name' : 'Next high export rate start', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.high_rate_export_end", state='undefined', attributes = {'friendly_name' : 'Next high export rate end', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.high_rate_export_cost", state=self.dp2(self.rate_export_average), attributes = {'friendly_name' : 'Next high export rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
        if len(self.high_export_rates) < 2 and not SIMULATE:
            self.set_state("predbat.high_rate_export_start_2", state='undefined', attributes = {'friendly_name' : 'Next+1 high export rate start', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.high_rate_export_end_2", state='undefined', attributes = {'friendly_name' : 'Next+1 high export rate end', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.high_rate_export_cost_2", state=self.dp2(self.rate_export_average), attributes = {'friendly_name' : 'Next+1 high export rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})


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
        minute = 0
        while minute < self.forecast_minutes:
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

    def rate_scan(self, rates, octopus_slots):
        """
        Scan the rates and work out min/max and charging windows
        """
        rate_low_min_window = 5
        rate_low_threshold = self.rate_low_threshold
        self.low_rates = []
        
        rate_min, rate_max, rate_average, rate_min_minute, rate_max_minute = self.rate_minmax(rates)
        self.log("Import rates min {} max {} average {}".format(rate_min, rate_max, rate_average))

        self.rate_min = rate_min
        self.rate_max = rate_max
        self.rate_min_minute = rate_min_minute
        self.rate_max_minute = rate_max_minute
        self.rate_average = rate_average
        self.rate_threshold = rate_average * rate_low_threshold
        if self.rate_low_match_export:
            # When enabled the low rate could be anything up-to the export rate (less battery losses)
            self.rate_threshold = max(self.rate_threshold, self.rate_export_max * self.battery_loss * self.battery_loss_discharge)

        # Add in any planned octopus slots
        if octopus_slots:
            for slot in octopus_slots:
                start = datetime.strptime(slot['startDtUtc'], TIME_FORMAT_OCTOPUS)
                end = datetime.strptime(slot['endDtUtc'], TIME_FORMAT_OCTOPUS)
                start_minutes = max(self.mintes_to_time(start, self.midnight_utc), 0)
                end_minutes   = min(self.mintes_to_time(end, self.midnight_utc), self.forecast_minutes)

                self.log("Octopus Intelligent slot at {}-{} assumed price {}".format(self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), rate_min))
                for minute in range(start_minutes, end_minutes):
                    rates[minute] = self.rate_min

        # Find charging window
        self.low_rates = self.rate_scan_window(rates, rate_low_min_window, self.rate_threshold, False)
        return rates

    def publish_rates_import(self):
        # Output rate info
        if self.low_rates:
            window_n = 0
            for window in self.low_rates:
                rate_low_start = window['start']
                rate_low_end = window['end']
                rate_low_average = window['average']

                self.log("Low import rate window:{} - {} to {} @{} !".format(window_n, self.time_abs_str(rate_low_start), self.time_abs_str(rate_low_end), rate_low_average))

                rate_low_start_date = self.midnight_utc + timedelta(minutes=rate_low_start)
                rate_low_end_date = self.midnight_utc + timedelta(minutes=rate_low_end)

                time_format_time = '%H:%M:%S'

                if window_n == 0 and not SIMULATE:
                    self.set_state("predbat.low_rate_start", state=rate_low_start_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next low rate start', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.low_rate_end", state=rate_low_end_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next low rate end', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.low_rate_cost", state=rate_low_average, attributes = {'friendly_name' : 'Next low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
                if window_n == 1 and not SIMULATE:
                    self.set_state("predbat.low_rate_start_2", state=rate_low_start_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next+1 low rate start', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.low_rate_end_2", state=rate_low_end_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next+1 low rate end', 'state_class': 'timestamp', 'icon': 'mdi:table-clock'})
                    self.set_state("predbat.low_rate_cost_2", state=rate_low_average, attributes = {'friendly_name' : 'Next+1 low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
                window_n += 1

        # Clear rates that aren't available
        if not self.low_rates and not SIMULATE:
            self.log("No low rate period found")
            self.set_state("predbat.low_rate_start", state='undefined', attributes = {'friendly_name' : 'Next low rate start', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.low_rate_end", state='undefined', attributes = {'friendly_name' : 'Next low rate end', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.low_rate_cost", state=self.rate_average, attributes = {'friendly_name' : 'Next low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
        if len(self.low_rates) < 2 and not SIMULATE:
            self.set_state("predbat.low_rate_start_2", state='undefined', attributes = {'friendly_name' : 'Next+1 low rate start', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.low_rate_end_2", state='undefined', attributes = {'friendly_name' : 'Next+1 low rate end', 'device_class': 'timestamp', 'icon': 'mdi:table-clock'})
            self.set_state("predbat.low_rate_cost_2", state=self.rate_average, attributes = {'friendly_name' : 'Next+1 low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})

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
                self.set_state("predbat.rates_export", state=self.dp2(rates[self.minutes_now]), attributes = {'min' : self.dp2(self.rate_export_min), 'max' : self.dp2(self.rate_export_max), 'average' : self.dp2(self.rate_export_average), 'threshold' : self.dp2(self.rate_export_threshold), 'results' : rates_time, 'friendly_name' : 'Export rates', 'state_class' : 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
            else:
                self.set_state("predbat.rates", state=self.dp2(rates[self.minutes_now]), attributes = {'min' : self.dp2(self.rate_min), 'max' : self.dp2(self.rate_max), 'average' : self.dp2(self.rate_average), 'threshold' : self.dp2(self.rate_threshold), 'results' : rates_time, 'friendly_name' : 'Import rates', 'state_class' : 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
        return rates

    def today_cost(self, import_today, export_today):
        """
        Work out energy costs today (approx)
        """
        day_cost = 0
        day_energy = 0
        day_energy_export = 0
        day_cost_time = {}

        for minute in range(0, self.minutes_now):
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
                
            if self.rate_export:
                day_cost -= self.rate_export[minute] * energy_export

            if (minute % 10) == 0:
                minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                day_cost_time[stamp] = self.dp2(day_cost)

        if not SIMULATE:
            self.set_state("predbat.cost_today", state=self.dp2(day_cost), attributes = {'results' : day_cost_time, 'friendly_name' : 'Cost so far today', 'state_class' : 'measurement', 'unit_of_measurement': 'p', 'icon': 'mdi:currency-usd'})
        self.log("Todays energy import {} kwh export {} kwh cost {} p".format(self.dp2(day_energy), self.dp2(day_energy_export), self.dp2(day_cost)))
        return day_cost

    def publish_discharge_limit(self, discharge_window, discharge_limits, best):
        """
        Create entity to chart discharge limit
        """
        discharge_limit_time = {}
        discharge_limit_time_kw = {}
        discharge_limit_soc = self.soc_max
        discharge_limit_percent = 100
        for minute in range(0, self.forecast_minutes + self.minutes_now, 30):
            window_n = self.in_charge_window(discharge_window, minute)
            minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            if window_n >=0 and (discharge_limits[window_n] < 100.0):
                discharge_limit_time[stamp] = discharge_limits[window_n]
                discharge_limit_time_kw[stamp] = (discharge_limits[window_n] * self.soc_max) / 100.0
                discharge_limit_soc = 0
                discharge_limit_percent = 0
            else:
                discharge_limit_time[stamp] = 100
                discharge_limit_time_kw[stamp] = self.soc_max

        if not SIMULATE:
            if best:
                self.set_state("predbat.best_discharge_limit_kw", state=self.dp2(discharge_limit_soc), attributes = {'results' : discharge_limit_time_kw, 'friendly_name' : 'Predicted discharge limit kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' :'mdi:battery-charging'})
                self.set_state("predbat.best_discharge_limit", state=discharge_limit_percent, attributes = {'results' : discharge_limit_time, 'friendly_name' : 'Predicted discharge limit best', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' :'mdi:battery-charging'})
            else:
                self.set_state("predbat.discharge_limit_kw", state=self.dp2(discharge_limit_soc), attributes = {'results' : discharge_limit_time_kw, 'friendly_name' : 'Predicted discharge limit kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' :'mdi:battery-charging'})
                self.set_state("predbat.discharge_limit", state=discharge_limit_percent, attributes = {'results' : discharge_limit_time, 'friendly_name' : 'Predicted discharge limit', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' :'mdi:battery-charging'})

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
            if charge_limit:
                # Ignore charge windows beyond 24 hours away as they won't apply right now
                if charge_window[0]['end'] < (24*60 + self.minutes_now):
                    charge_limit_first = charge_limit[0]
                    charge_limit_percent_first = charge_limit_percent[0]
            if best:
                self.set_state("predbat.best_charge_limit_kw", state=self.dp2(charge_limit_first), attributes = {'results' : charge_limit_time_kw, 'friendly_name' : 'Predicted charge limit kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' :'mdi:battery-charging'})
                self.set_state("predbat.best_charge_limit", state=charge_limit_percent_first, attributes = {'results' : charge_limit_time, 'friendly_name' : 'Predicted charge limit best', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' :'mdi:battery-charging'})
            else:
                self.set_state("predbat.charge_limit_kw", state=self.dp2(charge_limit_first), attributes = {'results' : charge_limit_time_kw, 'friendly_name' : 'Predicted charge limit kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'icon' :'mdi:battery-charging'})
                self.set_state("predbat.charge_limit", state=charge_limit_percent_first, attributes = {'results' : charge_limit_time, 'friendly_name' : 'Predicted charge limit', 'state_class': 'measurement', 'unit_of_measurement': '%', 'icon' :'mdi:battery-charging'})

    def reset(self):
        """
        Init stub
        """
        self.prediction_started = False
        self.update_pending = True
        self.midnight = None
        self.midnight_utc = None
        self.difference_minutes = 0
        self.minutes_now = 0
        self.minutes_to_midnight = 0
        self.days_previous = 0
        self.forecast_days = 0
        self.forecast_minutes = 0
        self.soc_kw = 0
        self.soc_max = 0
        self.predict_soc = {}
        self.predict_soc_best = {}
        self.metric_house = 0
        self.metric_battery = 0
        self.metric_export = 0
        self.metric_min_improvement = 0
        self.metric_min_improvement_discharge = 0
        self.rate_import = {}
        self.rate_export = {}
        self.rate_slots = []
        self.low_rates = []
        self.cost_today_sofar = 0
        self.octopus_slots = []
        self.car_charging_slots = []
        self.reserve = 0
        self.battery_loss = 1.0
        self.battery_loss_discharge = 1.0
        self.battery_scaling = 1.0
        self.best_soc_min = 0
        self.best_soc_margin = 0
        self.best_soc_keep = 0
        self.rate_min = 0
        self.rate_min_minute = 0
        self.rate_max = 0
        self.rate_max_minute = 0
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
        self.export_today = {}
        self.current_charge_limit = 0.0
        self.charge_window = []
        self.charge_limit = []
        self.charge_window_best = []
        self.charge_limit_best = []
        self.car_charging_battery_size = 100
        self.car_charging_limit = 100
        self.car_charging_soc = 0
        self.car_charging_rate = 7.4
        self.car_charging_loss = 1.0
        self.discharge_window = []
        self.discharge_limits = []
        self.discharge_limits_best = []
        self.discharge_window_best = []
        self.battery_rate_max = 0
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

    def optimise_charge_limit(self, window_n, record_charge_windows, try_charge_limit, charge_window, discharge_window, discharge_limits, load_minutes, pv_forecast_minute, pv_forecast_minute10, all_n = 0, end_record=None):
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
            metricmid, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute = self.run_prediction(try_charge_limit, charge_window, discharge_window, discharge_limits, load_minutes, pv_forecast_minute, end_record = end_record)

            # Simulate with 10% PV 
            metric10, charge_limit_percent10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10 = self.run_prediction(try_charge_limit, charge_window, discharge_window, discharge_limits, load_minutes, pv_forecast_minute10, end_record = end_record)

            # Store simulated mid value
            metric = metricmid
            cost = metricmid

            # Balancing payment to account for battery left over 
            # ie. how much extra battery is worth to us in future, assume it's the same as low rate
            metric -= soc * self.rate_min
            metric10 -= soc10 * self.rate_min

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
                    try_percent = try_soc / self.soc_max * 100.0
                if int(self.current_charge_limit) == int(try_percent):
                    metric -= max(0.1, self.metric_min_improvement)

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

    def optimise_discharge(self, window_n, record_charge_windows, try_charge_limit, charge_window, discharge_window, try_discharge, load_minutes, pv_forecast_minute, pv_forecast_minute10, all_n = False, end_record=None):
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
        best_start = window['start']
        
        for loop_limit in [100, 0]:
            for loop_start in range(window['start'], window['end'], self.discharge_slot_split):

                this_discharge_limit = loop_limit
                start = loop_start

                # Can't optimise all window start slot
                if all_n and (start != window['start']):
                    continue

                # Don't optimise start of disabled windows
                if (this_discharge_limit == 100.0) and (start != window['start']):
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
                    start = min(start, window['end'] - self.discharge_slot_split)
                    try_discharge_window[window_n]['start'] = start

                was_debug = self.debug_enable
                self.debug_enable = False

                # Simulate with medium PV
                metricmid, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute = self.run_prediction(try_charge_limit, charge_window, try_discharge_window, try_discharge, load_minutes, pv_forecast_minute, end_record = end_record)

                # Simulate with 10% PV 
                metric10, charge_limit_percent10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10 = self.run_prediction(try_charge_limit, charge_window, try_discharge_window, try_discharge, load_minutes, pv_forecast_minute10, end_record = end_record)

                # Put back debug enable
                self.debug_enable = was_debug

                # Store simulated mid value
                metric = metricmid
                cost = metricmid

                # Balancing payment to account for battery left over 
                # ie. how much extra battery is worth to us in future, assume it's the same as low rate
                metric -= soc * self.rate_min
                metric10 -= soc10 * self.rate_min

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
                            metric -= max(0.1, self.metric_min_improvement_discharge)

                if self.debug_enable:
                    self.log("Sim: Discharge {} window {} start {} imp bat {} house {} exp {} min_soc {} @ {} soc {} cost {} metric {} metricmid {} metric10 {} end_record {}".format
                            (this_discharge_limit, window_n, try_discharge_window[window_n]['start'], self.dp2(import_kwh_battery), self.dp2(import_kwh_house), self.dp2(export_kwh), self.dp2(soc_min), self.time_abs_str(soc_min_minute), self.dp2(soc), self.dp2(cost), self.dp2(metric), self.dp2(metricmid), self.dp2(metric10), end_record))

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
        return window['key']

    def window_sort_func_start(self, window):
        """
        Helper sort index function
        """
        return window['start']

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
                window['key'] = "%04.2f.%02d" % (1000 - window['average'], 99 - window['id'])
            else:
                window['key'] = "%04.2f.%02d" % (1000 - window['average'], window['id'])
            wid += 1
        window_with_id.sort(key=self.window_sort_func)
        id_list = []
        for window in window_with_id:
            id_list.append(window['id'])
        #  self.log("Sorted window list {} ids {}".format(window_with_id, id_list))
        return id_list

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
            if (charge_limit_best[window_n] > self.dp2(reserve)) or (self.minutes_now >= start and self.minutes_now < end and self.charge_limit and self.charge_limit[0]['end'] == end):
                new_limit_best.append(charge_limit_best[window_n])
                new_window_best.append(charge_window_best[window_n])
        return new_limit_best, new_window_best 

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
                predict_minute = int((window_start - minutes_now) / 5) * 5
                soc = predict_soc[predict_minute]

                if self.debug_enable:
                    self.log("Examine window {} from {} - {} limit {} - starting soc {}".format(window_n, window_start, window_end, limit, soc))

                # Discharge level adjustments for safety
                predict_minute = int((window_end - minutes_now) / 5) * 5
                soc = predict_soc[predict_minute]
                if soc > limit_soc:
                    # Give it 10 minute margin
                    limit_soc = max(limit_soc, soc - 10 * self.battery_rate_max)
                    discharge_limits_best[window_n] = float(int(limit_soc / self.soc_max * 100.0 + 0.5))
                    if limit != discharge_limits_best[window_n]:
                        self.log("Clip discharge window {} from {} - {} from limit {} to new limit {}".format(window_n, window_start, window_end, limit, discharge_limits_best[window_n]))

            else:
                self.log("WARN: Clip discharge window {} as it's already passed".format(window_n))
                discharge_limits_best[window_n] = 100

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
                else:
                    new_best.append(discharge_window_best[window_n])
                    new_enable.append(discharge_limits_best[window_n])

        return new_enable, new_best
    
    def optimise_discharge_windows(self, end_record, load_minutes, pv_forecast_minute, pv_forecast_minute10):
        """
        Optimize the discharge windows
        """
        # Try different discharge options
        if self.discharge_window_best and self.calculate_best_discharge:
            record_discharge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.discharge_window_best), 1)

            # Set all to off
            self.discharge_limits_best = [100.0 for n in range(0, len(self.discharge_window_best))]

            # First do rough optimisation of all windows
            if self.calculate_discharge_all and record_discharge_windows > 1:
                
                self.log("Optimise all discharge windows n={}".format(record_discharge_windows))
                best_discharge, best_start, best_metric, best_cost, soc_min, soc_min_minute = self.optimise_discharge(0, record_discharge_windows, self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, load_minutes, pv_forecast_minute, pv_forecast_minute10, all_n = True, end_record = end_record)

                self.discharge_limits_best = [best_discharge if n < record_discharge_windows else 100.0 for n in range(0, len(self.discharge_limits_best))]
                self.log("Best all discharge limit all windows n={} (adjusted) discharge limit {} min {} @ {} (margin added {} and min {}) with metric {} cost {} windows {}".format(record_discharge_windows, best_discharge, self.dp2(soc_min), self.time_abs_str(soc_min_minute), self.best_soc_margin, self.best_soc_min, self.dp2(best_metric), self.dp2(best_cost), self.charge_limit_best))

            # Optimise in price order, most expensive first try to increase each one
            for discharge_pass in range(0, self.calculate_discharge_passes):
                self.log("Optimise discharge pass {}".format(discharge_pass))
                price_sorted = self.sort_window_by_price(self.discharge_window_best[:record_discharge_windows], reverse_time=self.calculate_discharge_oldest)
                for window_n in price_sorted:
                    best_discharge, best_start, best_metric, best_cost, soc_min, soc_min_minute = self.optimise_discharge(window_n, record_discharge_windows, self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, load_minutes, pv_forecast_minute, pv_forecast_minute10, end_record = end_record)

                    self.discharge_limits_best[window_n] = best_discharge
                    self.discharge_window_best[window_n]['start'] = best_start

                    if self.debug_enable or 1:
                        self.log("Best discharge limit window {} time {} - {} discharge {} (adjusted) min {} @ {} (margin added {} and min {}) with metric {} cost {}".format(window_n, self.discharge_window_best[window_n]['start'], self.discharge_window_best[window_n]['end'], best_discharge, self.dp2(soc_min), self.time_abs_str(soc_min_minute), self.best_soc_margin, self.best_soc_min, self.dp2(best_metric), self.dp2(best_cost)))


    def optimise_charge_windows_reset(self, end_record, load_minutes, pv_forecast_minute, pv_forecast_minute10):
        """
        Reset the charge windows to max
        """
        if self.charge_window_best and self.calculate_best_charge:
            # Set all to max
            self.charge_limit_best = [self.soc_max for n in range(0, len(self.charge_limit_best))]

    def optimise_charge_windows(self, end_record, load_minutes, pv_forecast_minute, pv_forecast_minute10):
        """
        Optimise the charge windows
        """
        if self.charge_window_best and self.calculate_best_charge:
            record_charge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.charge_window_best), 1)
            self.log("Record charge windows is {} end_record_abs was {}".format(record_charge_windows, self.time_abs_str(end_record + self.minutes_now)))
            # Set all to min
            self.charge_limit_best = [self.reserve if n < record_charge_windows else self.soc_max for n in range(0, len(self.charge_limit_best))]

            if self.calculate_charge_all or record_charge_windows==1:
                # First do rough optimisation of all windows
                self.log("Optimise all charge windows n={}".format(record_charge_windows))
                best_soc, best_metric, best_cost, soc_min, soc_min_minute = self.optimise_charge_limit(0, record_charge_windows, self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, load_minutes, pv_forecast_minute, pv_forecast_minute10, all_n = record_charge_windows, end_record = end_record)
                if record_charge_windows > 1:
                    best_soc = min(best_soc + self.best_soc_pass_margin, self.soc_max)

                # Set all to optimisation
                self.charge_limit_best = [best_soc if n < record_charge_windows else self.soc_max for n in range(0, len(self.charge_limit_best))]
                self.log("Best all charge limit all windows n={} (adjusted) soc calculated at {} min {} @ {} (margin added {} and min {}) with metric {} cost {} windows {}".format(record_charge_windows, self.dp2(best_soc), self.dp2(soc_min), self.time_abs_str(soc_min_minute), self.best_soc_margin, self.best_soc_min, self.dp2(best_metric), self.dp2(best_cost), self.charge_limit_best))

            if record_charge_windows > 1:
                for charge_pass in range(0, self.calculate_charge_passes):
                    self.log("Optimise charge pass {}".format(charge_pass))
                    # Optimise in price order, most expensive first try to reduce each one, only required for more than 1 window
                    price_sorted = self.sort_window_by_price(self.charge_window_best[:record_charge_windows], reverse_time=self.calculate_charge_oldest)
                    for window_n in price_sorted:
                        best_soc, best_metric, best_cost, soc_min, soc_min_minute = self.optimise_charge_limit(window_n, record_charge_windows, self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, load_minutes, pv_forecast_minute, pv_forecast_minute10, end_record = end_record)

                        self.charge_limit_best[window_n] = best_soc
                        if self.debug_enable or 1:
                            self.log("Best charge limit window {} (adjusted) soc calculated at {} min {} @ {} (margin added {} and min {}) with metric {} cost {} windows {}".format(window_n, self.dp2(best_soc), self.dp2(soc_min), self.time_abs_str(soc_min_minute), self.best_soc_margin, self.best_soc_min, self.dp2(best_metric), self.dp2(best_cost), self.charge_limit_best))


    def update_pred(self):
        """
        Update the prediction state, everything is called from here right now
        """
        local_tz = pytz.timezone(self.get_arg('timezone', "Europe/London"))
        now_utc = datetime.now(local_tz)
        now = datetime.now()
        if SIMULATE:
            now += timedelta(minutes=self.simulate_offset)
            now_utc += timedelta(minutes=self.simulate_offset)

        self.log("--------------- PredBat - update at: " + str(now_utc))

        self.debug_enable = self.get_arg('debug_enable', False)
        self.max_windows = self.get_arg('max_windows', 24)

        self.log("Debug enable is {}".format(self.debug_enable))

        self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        self.difference_minutes = self.minutes_since_yesterday(now)
        self.minutes_now = int((now - self.midnight).seconds / 60)
        self.minutes_to_midnight = 24*60 - self.minutes_now

        self.days_previous = self.get_arg('days_previous', [7])
        self.max_days_previous = max(self.days_previous) + 1

        forecast_hours = self.get_arg('forecast_hours', 48)
        self.forecast_days = int((forecast_hours + 23)/24)
        self.forecast_minutes = forecast_hours * 60
        self.forecast_plan_hours = self.get_arg('forecast_plan_hours', 24)

        # Metric config
        self.metric_house = self.get_arg('metric_house', 38.0)
        self.metric_battery = self.get_arg('metric_battery', 7.5)
        self.metric_export = self.get_arg('metric_export', 4.0)
        self.metric_min_improvement = self.get_arg('metric_min_improvement', 0.0)
        self.metric_min_improvement_discharge = self.get_arg('metric_min_improvement_discharge', 0.0)
        self.notify_devices = self.get_arg('notify_devices', ['notify'])
        self.pv_scaling = self.get_arg('pv_scaling', 1.0)
        self.pv_metric10_weight = self.get_arg('pv_metric10_weight', 0.0)
        self.load_scaling = self.get_arg('load_scaling', 1.0)
        self.battery_rate_max_scaling = self.get_arg('battery_rate_max_scaling', 1.0)
        self.best_soc_pass_margin = self.get_arg('best_soc_pass_margin', 0.0)
        self.rate_low_threshold = self.get_arg('rate_low_threshold', 0.8)
        self.rate_high_threshold = self.get_arg('rate_high_threshold', 1.2)
        self.rate_low_match_export = self.get_arg('rate_low_match_export', False)
        self.best_soc_step = self.get_arg('best_soc_step', 0.5)

        # Battery charging options
        self.battery_loss = 1.0 - self.get_arg('battery_loss', 0.05)
        self.battery_loss_discharge = 1.0 - self.get_arg('battery_loss_discharge', 0.0)
        self.battery_scaling = self.get_arg('battery_scaling', 1.0)
        self.import_export_scaling = self.get_arg('import_export_scaling', 1.0)
        self.best_soc_margin = self.get_arg('best_soc_margin', 0.0)
        self.best_soc_min = self.get_arg('best_soc_min', 0.5)
        self.best_soc_keep = self.get_arg('best_soc_keep', 0.5)
        self.set_soc_minutes = self.get_arg('set_soc_minutes', 30)
        self.set_window_minutes = self.get_arg('set_window_minutes', 30)
        self.octopus_intelligent_charging = self.get_arg('octopus_intelligent_charging', True)
        self.car_charging_planned = self.get_arg('car_charging_planned', "no")
        self.log("Car charging planned returns {}".format(self.car_charging_planned))
        if isinstance(self.car_charging_planned , str):
            if self.car_charging_planned .lower() in self.get_arg('car_charging_planned_response', ['yes', 'on', 'enable', 'true']):
                self.car_charging_planned  = True
            else:
                self.car_charging_planned  = False
        self.car_charging_plan_smart = self.get_arg('car_charging_plan_smart', False)
        self.car_charging_plan_time = self.get_arg('car_charging_plan_time', "07:00:00")
       
        self.combine_mixed_rates = self.get_arg('combine_mixed_rates', False)
        self.combine_discharge_slots = self.get_arg('combine_discharge_slots', True)
        self.combine_charge_slots = self.get_arg('combine_charge_slots', True)
        self.discharge_slot_split = self.get_arg('discharge_slot_split', 15)
        self.charge_slot_split = self.get_arg('charge_slot_split', 30)
        self.calculate_charge_passes = self.get_arg('calculate_charge_passes', 1)
        self.calculate_discharge_passes = self.get_arg('calculate_discharge_passes', 1)

        # Enables
        self.calculate_best = self.get_arg('calculate_best', False)
        self.set_soc_enable = self.get_arg('set_soc_enable', False)
        self.set_reserve_enable = self.get_arg('set_reserve_enable', False)
        self.set_reserve_notify = self.get_arg('set_reserve_notify', False)
        self.set_soc_notify = self.get_arg('set_soc_notify', False)
        self.set_window_notify = self.get_arg('set_window_notify', False)
        self.set_charge_window = self.get_arg('set_charge_window', False)
        self.set_discharge_window = self.get_arg('set_discharge_window', False)
        self.set_discharge_notify = self.get_arg('set_discharge_notify', False)
        self.calculate_best_charge = self.get_arg('calculate_best_charge', True)
        self.calculate_charge_oldest = self.get_arg('calculate_charge_oldest', False)
        self.calculate_charge_all = self.get_arg('calculate_charge_all', True)
        self.calculate_best_discharge = self.get_arg('calculate_best_discharge', self.set_discharge_window)
        self.calculate_discharge_oldest = self.get_arg('calculate_discharge_oldest', True)
        self.calculate_discharge_all = self.get_arg('calculate_discharge_all', False)
        self.calculate_discharge_first = self.get_arg('calculate_discharge_first', True)

        # Car options
        self.car_charging_hold = self.get_arg('car_charging_hold', False)
        self.car_charging_threshold = float(self.get_arg('car_charging_threshold', 6.0)) / 60.0
        self.car_charging_energy_scale = self.get_arg('car_charging_energy_scale', 1.0)

        self.rate_import = {}
        self.rate_export = {}
        self.rate_slots = []
        self.low_rates = []
        self.octopus_slots = []
        self.car_charging_slots = []
        self.cost_today_sofar = 0
        self.import_today = {}
        self.export_today = {}
        self.load_minutes = {}

        # Load previous load data
        if self.get_arg('ge_cloud_data', False):
            self.download_ge_data(now_utc)
        else:
            # Load data
            if 'load_today' in self.args:
                self.load_minutes = self.minute_data_load(now_utc)
            else:
                self.log("WARN: You have not set load_today, you will have no load data")

            # Load import today data 
            if 'import_today' in self.args:
                self.import_today = self.minute_data_import_export(now_utc, 'import_today')
            else:
                self.log("WARN: You have not set import_today, you will have no previous import data")

            # Load export today data 
            if 'export_today' in self.args:
                self.export_today = self.minute_data_import_export(now_utc, 'export_today')
            else:
                self.log("WARN: You have not set export_today, you will have no previous export data")

        # Car charging information
        self.car_charging_battery_size = float(self.get_arg('car_charging_battery_size', 100.0))
        self.car_charging_rate = (float(self.get_arg('car_charging_rate', 7.4)))

        # Basic rates defined by user over time
        if 'rates_import' in self.args:
            self.rate_import = self.basic_rates(self.get_arg('rates_import', indirect=False), 'import')
        if 'rates_export' in self.args:
            self.rate_export = self.basic_rates(self.get_arg('rates_export', indirect=False), 'export')

        # Octopus import rates
        if 'metric_octopus_import' in self.args:
            data_import = self.get_state(entity_id = self.get_arg('metric_octopus_import', indirect=False), attribute='rates')
            if data_import:
                self.rate_import = self.minute_data(data_import, self.forecast_days + 1, self.midnight_utc, 'rate', 'from', backwards=False, to_key='to')
            else:
                self.log("Warning: metric_octopus_import is not set correctly, ignoring..")

        # Work out current car SOC and limit
        self.car_charging_limit = (float(self.get_arg('car_charging_limit', 100.0)) * self.car_charging_battery_size) / 100.0
        self.car_charging_loss = 1 - float(self.get_arg('car_charging_loss', 0.0))

        # Octopus intelligent slots
        if 'octopus_intelligent_slot' in self.args:
            entity_id = self.get_arg('octopus_intelligent_slot', indirect=False)
            completed = self.get_state(entity_id = entity_id, attribute='completedDispatches')
            if completed:
                self.octopus_slots += completed
            planned = self.get_state(entity_id = entity_id, attribute='plannedDispatches')
            if planned:
                self.octopus_slots += planned

            # Extract vehicle data if we can get it
            vehicle = self.get_state(entity_id = entity_id, attribute='registeredKrakenflexDevice')
            if vehicle:
                self.car_charging_battery_size = float(vehicle.get('vehicleBatterySizeInKwh', self.car_charging_battery_size))
                self.car_charging_rate = float(vehicle.get('chargePointPowerInKw', self.car_charging_rate))

            # Get car charging limit again from car based on new battery size
            self.car_charging_limit = (float(self.get_arg('car_charging_limit', 100.0)) * self.car_charging_battery_size) / 100.0

            # Extract vehicle preference if we can get it
            vehicle_pref = self.get_state(entity_id = entity_id, attribute='vehicleChargingPreferences')            
            if vehicle_pref and self.octopus_intelligent_charging:
                octopus_limit = max(float(vehicle_pref.get('weekdayTargetSoc', 100)), float(vehicle_pref.get('weekendTargetSoc', 100)))
                octopus_ready_time = vehicle_pref.get('weekdayTargetTime', None)
                if not octopus_ready_time:
                    octopus_ready_time = self.car_charging_plan_time
                else:
                    octopus_ready_time += ":00"
                self.car_charging_plan_time = octopus_ready_time
                octopus_limit = self.dp2(octopus_limit * self.car_charging_battery_size / 100.0)
                self.log("Car charging limit {} and Octopus limit {} - select min - battery size {}".format(self.car_charging_limit, octopus_limit, self.car_charging_battery_size))
                self.car_charging_limit = min(self.car_charging_limit, octopus_limit)
            
            # Use octopus slots for charging?
            if self.octopus_intelligent_charging:
                self.car_charging_slots = self.load_octopus_slots(self.octopus_slots)
        else:
            # Disable octopus charging if we don't have the slot sensor
            self.octopus_intelligent_charging = False

        # Work out car SOC
        self.car_charging_soc = (self.get_arg('car_charging_soc', 0.0) * self.car_charging_battery_size) / 100.0

        # Fixed URL for rate import
        if 'rates_import_octopus_url' in self.args:
            self.log("Downloading import rates directly from url {}".format(self.get_arg('rates_import_octopus_url', indirect=False)))
            self.rate_import = self.download_octopus_rates(self.get_arg('rates_import_octopus_url', indirect=False))

        # Octopus export rates
        if 'metric_octopus_export' in self.args:
            data_export = self.get_state(entity_id = self.get_arg('metric_octopus_export', indirect=False), attribute='rates')
            if data_export:
                self.rate_export = self.minute_data(data_export, self.forecast_days + 1, self.midnight_utc, 'rate', 'from', backwards=False, to_key='to')
            else:
                self.log("Warning: metric_octopus_export is not set correctly, ignoring..")

        # Fixed URL for rate export
        if 'rates_export_octopus_url' in self.args:
            self.log("Downloading export rates directly from url {}".format(self.get_arg('rates_export_octopus_url', indirect=False)))
            self.rate_export = self.download_octopus_rates(self.get_arg('rates_export_octopus_url', indirect=False))

        # Replicate and scan export rates
        if self.rate_export:
            self.rate_export = self.rate_replicate(self.rate_export)
            self.rate_export = self.rate_scan_export(self.rate_export)
            self.publish_rates(self.rate_export, True)
        else:
            self.log("No export rate data provided - using default metric")

        # Replicate and scan import rates
        if self.rate_import:
            self.rate_import = self.rate_replicate(self.rate_import)
            self.rate_import = self.rate_scan(self.rate_import, self.octopus_slots)
            self.publish_rates(self.rate_import, False)
        else:
            self.log("No import rate data provided - using default metric")

        # Log vehicle info
        if self.car_charging_planned or ('octopus_intelligent_slot' in self.args):
            self.log('Vehicle details: battery size {} rate {} limit {} current soc {}'.format(self.car_charging_battery_size, self.car_charging_rate, self.car_charging_limit, self.car_charging_soc))

        # Work out car plan?
        if self.car_charging_planned and not self.octopus_intelligent_charging:
            self.log("Plan car charging from {} to {} with slots {} from soc {} to {} ready by {}".format(self.car_charging_soc, self.car_charging_limit, self.low_rates, self.car_charging_soc, self.car_charging_limit, self.car_charging_plan_time))
            self.car_charging_slots = self.plan_car_charging(self.low_rates)
        else:
            if self.octopus_intelligent_charging:
                self.log("Not planning car charging, Octopus intelligent is enabled, check it's scheduling first")
            else:
                self.log("Not planning car charging - car charging planned is False")

        # Log the charging plan
        if self.car_charging_slots:
            self.log("Car charging plan is: {}".format(self.car_charging_slots))

        # Publish the car plan
        self.publish_car_plan()

        # Work out cost today
        if self.import_today:
            self.cost_today_sofar = self.today_cost(self.import_today, self.export_today)

        # Find the inverters
        self.num_inverters = int(self.get_arg('num_inverters', 1))
        self.inverter_limit = 0.0
        self.inverters = []
        self.charge_window = []
        self.discharge_window = []
        self.discharge_limits = []
        self.current_charge_limit = 0.0
        self.soc_kw = 0.0
        self.soc_max = 0.0
        self.reserve = 0.0
        self.battery_rate_max = 0.0
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
            self.battery_rate_max += inverter.battery_rate_max
            self.charge_rate_max += inverter.charge_rate_max
            self.discharge_rate_max += inverter.discharge_rate_max
            self.inverters.append(inverter)
            self.inverter_limit += inverter.inverter_limit

        # Remove extra decimals
        self.soc_max = self.dp2(self.soc_max)
        self.soc_kw = self.dp2(self.soc_kw)
        self.log("Found {} inverters total reserve {} soc_max {} soc {} charge rate {} kw discharge rate {} kw ac limit {} kw".format(len(self.inverters), self.reserve, self.soc_max, self.soc_kw, self.charge_rate_max * 60, self.discharge_rate_max * 60, self.dp2(self.inverter_limit * 60)))

        # Work out current charge limits
        self.charge_limit = [self.current_charge_limit * self.soc_max / 100.0 for i in range(0, len(self.charge_window))]
        self.charge_limit_percent = [self.current_charge_limit for i in range(0, len(self.charge_window))]

        self.log("Base charge limit {} window {} percent {}".format(self.charge_limit, self.charge_window, self.charge_limit_percent))
        self.log("Base discharge limit {} window {}".format(self.discharge_limits, self.discharge_window))

        # Calculate best charge windows
        if self.low_rates:
            # If we are using calculated windows directly then save them
            self.charge_window_best = copy.deepcopy(self.low_rates)
            self.log('Charge windows best will be {}'.format(self.charge_window_best))
        else:
            # Default best charge window as this one
            self.charge_window_best = self.charge_window

        # Calculate best discharge windows
        if self.high_export_rates:
            self.discharge_window_best = copy.deepcopy(self.high_export_rates)
            self.log('Discharge windows best will be {}'.format(self.discharge_window_best))
        else:
            self.discharge_window_best = []

        # Pre-fill best charge limit with the current charge limit
        self.charge_limit_best = [self.current_charge_limit * self.soc_max / 100.0 for i in range(0, len(self.charge_window_best))]
        self.charge_limit_percent_best = [self.current_charge_limit for i in range(0, len(self.charge_window_best))]

        # Pre-fill best discharge enable with Off
        self.discharge_limits_best = [100.0 for i in range(0, len(self.discharge_window_best))]

        # Fetch PV forecast if enbled, today must be enabled, other days are optional
        if 'pv_forecast_today' in self.args:
            pv_forecast_data    = self.get_state(entity_id = self.get_arg('pv_forecast_today', indirect=False), attribute='detailedForecast')
            if 'pv_forecast_tomorrow' in self.args:
                pv_forecast_data += self.get_state(entity_id = self.get_arg('pv_forecast_tomorrow', indirect=False), attribute='detailedForecast')
            if 'pv_forecast_d3' in self.args:
                pv_forecast_data += self.get_state(entity_id = self.get_arg('pv_forecast_d3', indirect=False), attribute='detailedForecast')
            if 'pv_forecast_d4' in self.args:
                pv_forecast_data += self.get_state(entity_id = self.get_arg('pv_forecast_d4', indirect=False), attribute='detailedForecast')
            if 'pv_forecast_d5' in self.args:
                pv_forecast_data += self.get_state(entity_id = self.get_arg('pv_forecast_d5', indirect=False), attribute='detailedForecast')
            if 'pv_forecast_d6' in self.args:
                pv_forecast_data += self.get_state(entity_id = self.get_arg('pv_forecast_d6', indirect=False), attribute='detailedForecast')
            if 'pv_forecast_d7' in self.args:
                pv_forecast_data += self.get_state(entity_id = self.get_arg('pv_forecast_d7', indirect=False), attribute='detailedForecast')
            pv_forecast_minute = self.minute_data(pv_forecast_data, self.forecast_days + 1, self.midnight_utc, 'pv_estimate' + str(self.get_arg('pv_estimate', '')), 'period_start', backwards=False, divide_by=30, scale=self.pv_scaling)
            pv_forecast_minute10 = self.minute_data(pv_forecast_data, self.forecast_days + 1, self.midnight_utc, 'pv_estimate10', 'period_start', backwards=False, divide_by=30, scale=self.pv_scaling)
        else:
            pv_forecast_minute = {}
            pv_forecast_minute10 = {}

        # Car charging hold - when enabled battery is held during car charging in simulation
        self.car_charging_energy = {}
        if 'car_charging_energy' in self.args:
            history = self.get_history(entity_id = self.get_arg('car_charging_energy', indirect=False), days = self.max_days_previous)
            if history:
                self.car_charging_energy = self.minute_data(history[0], self.max_days_previous, now_utc, 'state', 'last_updated', backwards=True, smoothing=True, clean_increment=True, scale=self.car_charging_energy_scale)
                self.log("Car charging hold {} with energy data".format(self.car_charging_hold))
        else:
            self.log("Car charging hold {} threshold {}".format(self.car_charging_hold, self.car_charging_threshold*60.0))

        # Simulate current settings
        end_record = self.record_length(self.charge_window_best)
        metric, self.charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute = self.run_prediction(self.charge_limit, self.charge_window, self.discharge_window, self.discharge_limits, self.load_minutes, pv_forecast_minute, save='base', end_record=end_record)

        # Try different battery SOCs to get the best result
        if self.calculate_best:
            if self.calculate_discharge_first:
                self.log("Calculate discharge first is set")
                self.optimise_charge_windows_reset(end_record, self.load_minutes, pv_forecast_minute, pv_forecast_minute10)
                self.optimise_discharge_windows(end_record, self.load_minutes, pv_forecast_minute, pv_forecast_minute10)
                self.optimise_charge_windows(end_record, self.load_minutes, pv_forecast_minute, pv_forecast_minute10)
            else:
                self.optimise_charge_windows(end_record, self.load_minutes, pv_forecast_minute, pv_forecast_minute10)
                self.optimise_discharge_windows(end_record, self.load_minutes, pv_forecast_minute, pv_forecast_minute10)

            # Filter out any unused charge windows
            if self.set_charge_window:
                self.charge_limit_best, self.charge_window_best = self.discard_unused_charge_slots(self.charge_limit_best, self.charge_window_best, self.reserve)
                self.log("Filtered charge windows {} {} reserve {}".format(self.charge_limit_best, self.charge_window_best, self.reserve))
            else:
                self.log("Unfiltered charge windows {} {} reserve {}".format(self.charge_limit_best, self.charge_window_best, self.reserve))

            # Filter out any unused discharge windows
            if self.set_discharge_window and self.discharge_window_best:
                # Filter out the windows we disabled
                self.discharge_limits_best, self.discharge_window_best = self.discard_unused_discharge_slots(self.discharge_limits_best, self.discharge_window_best)

                # Clipping windows
                if self.discharge_window_best:
                    # Work out new record end
                    # end_record = self.record_length(self.charge_window_best)
                    record_discharge_windows = max(self.max_charge_windows(end_record + self.minutes_now, self.discharge_window_best), 1)

                    # Discharge slot clipping
                    self.clip_discharge_slots(self.minutes_now, self.predict_soc, self.discharge_window_best, self.discharge_limits_best, record_discharge_windows, PREDICT_STEP) 

                    # Filter out the windows we disabled during clipping
                    self.discharge_limits_best, self.discharge_window_best = self.discard_unused_discharge_slots(self.discharge_limits_best, self.discharge_window_best)
                self.log("Discharge windows now {} {}".format(self.discharge_limits_best, self.discharge_window_best))
        
            # Final simulation of best, do 10% and normal scenario
            best_metric10, self.charge_limit_percent_best10, import_kwh_battery10, import_kwh_house10, export_kwh10, soc_min10, soc10, soc_min_minute10 = self.run_prediction(self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, self.load_minutes, pv_forecast_minute10, save='best10', end_record=end_record)
            best_metric, self.charge_limit_percent_best, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute = self.run_prediction(self.charge_limit_best, self.charge_window_best, self.discharge_window_best, self.discharge_limits_best, self.load_minutes, pv_forecast_minute, save='best', end_record=end_record)
            self.log("Best charging limit socs {} export {} gives import battery {} house {} export {} metric {} metric10 {}".format
            (self.charge_limit_best, self.discharge_limits_best, self.dp2(import_kwh_battery), self.dp2(import_kwh_house), self.dp2(export_kwh), self.dp2(best_metric), self.dp2(best_metric10)))

            # Publish charge and discharge window best
            self.publish_charge_limit(self.charge_limit_best, self.charge_window_best, self.charge_limit_percent_best, best=True)
            self.publish_discharge_limit(self.discharge_window_best, self.discharge_limits_best, best=True)

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
                    self.log("Include original charge start {} with our start which is {}".format(inverter.charge_start_time_minutes, minutes_start))
                    minutes_start = inverter.charge_start_time_minutes

                # Check if end is within 24 hours of now and end is in the future
                if (minutes_end - self.minutes_now) < 24*60 and minutes_end > self.minutes_now:
                    charge_start_time = self.midnight_utc + timedelta(minutes=minutes_start)
                    charge_end_time = self.midnight_utc + timedelta(minutes=minutes_end)
                    self.log("Charge window will be: {} - {}".format(charge_start_time, charge_end_time))

                    # Are we actually charging?
                    if self.minutes_now >= minutes_start and self.minutes_now < minutes_end:
                        inverter.adjust_charge_rate(inverter.battery_rate_max * 60 * 1000)
                        status = "Charging"

                    # We must re-program if we are about to start a new charge window
                    # or the currently configured window is about to start but hasn't yet started (don't change once it's started)
                    if (self.minutes_now < minutes_end) and (
                        (minutes_start - self.minutes_now) <= self.set_window_minutes or 
                        (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_window_minutes
                        ):
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
                    self.log("Include original discharge start {} with our start which is {}".format(inverter.discharge_start_time_minutes, minutes_start))
                    minutes_start = inverter.discharge_start_time_minutes

                discharge_start_time = self.midnight_utc + timedelta(minutes=minutes_start)
                discharge_end_time = self.midnight_utc + timedelta(minutes=minutes_end)
                discharge_soc = (self.discharge_limits_best[0] * self.soc_max) / 100.0
                self.log("Next discharge window will be: {} - {} at reserve {}".format(discharge_start_time, discharge_end_time, self.discharge_limits_best[0]))
                if (self.minutes_now >= minutes_start) and (self.minutes_now < minutes_end) and (self.discharge_limits_best[0] < 100.0):
                    if (self.soc_kw - PREDICT_STEP * inverter.battery_rate_max) > discharge_soc:
                        self.log("Discharging now - current SOC {} and target {}".format(self.soc_kw, discharge_soc))
                        inverter.adjust_discharge_rate(inverter.battery_rate_max * 60 * 1000)
                        inverter.adjust_force_discharge(True, discharge_start_time, discharge_end_time)
                        if self.set_reserve_enable:
                            inverter.adjust_reserve(self.discharge_limits_best[0])
                            setReserve = True
                        status = "Discharging"
                    else:
                        self.log("Setting ECO mode as discharge is now at/below target - current SOC {} and target {}".format(self.soc_kw, discharge_soc))
                        inverter.adjust_force_discharge(False)
                        status = "Hold discharging"
                        resetReserve = True
                else:
                    if (self.minutes_now < minutes_end) and ((minutes_start - self.minutes_now) <= self.set_window_minutes) and self.discharge_limits_best[0]:
                        inverter.adjust_force_discharge(False, discharge_start_time, discharge_end_time)
                        resetReserve = True
                    else:
                        self.log("Setting ECO mode as we are not yet within the discharge window - next time is {} - {}".format(self.time_abs_str(minutes_start), self.time_abs_str(minutes_end)))
                        inverter.adjust_force_discharge(False)
                        resetReserve = True
            elif self.set_discharge_window:
                self.log("Setting ECO mode as no discharge window planned")
                inverter.adjust_force_discharge(False)
                resetReserve = True
            
            # Set the SOC just before or within the charge window
            if self.set_soc_enable:
                if self.charge_limit_best and (self.minutes_now < inverter.charge_end_time_minutes) and (inverter.charge_start_time_minutes - self.minutes_now) <= self.set_soc_minutes:
                    inverter.adjust_battery_target(self.charge_limit_percent_best[0])
                else:
                    self.log("Not setting charging SOC as we are not within the window (now {} target set_soc_minutes {} charge start time {}".format(self.time_abs_str(self.minutes_now), self.set_soc_minutes, self.time_abs_str(inverter.charge_start_time_minutes)))

            # If we should set reserve?
            if self.set_soc_enable and self.set_reserve_enable and not setReserve:
                # In the window then set it, otherwise put it back
                if self.charge_limit_best and (self.minutes_now < inverter.charge_end_time_minutes) and (self.minutes_now >= inverter.charge_start_time_minutes):
                    self.log("Adjust reserve to target charge % (as set_reserve_enable is true".format(self.charge_limit_percent_best[0]))
                    inverter.adjust_reserve(self.charge_limit_percent_best[0])
                    resetReserve = False
                else:
                    self.log("Adjust reserve to default (as set_reserve_enable is true)")
                    inverter.adjust_reserve(0)
                    resetReserve = False
            
            # Reset reserve as discharge is enable but not running right now
            if self.set_reserve_enable and resetReserve and not setReserve:
                inverter.adjust_reserve(0)

        self.log("Completed run status {}".format(status))
        self.record_status(status, debug="best_soc={} window={} discharge={}".format(self.charge_limit_best, self.charge_window_best,self.discharge_window_best))

    def select_event(self, event, data, kwargs):
        """
        Catch HA Input select updates
        """
        value = data['service_data']['option']
        entities = data['service_data']['entity_id']

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
        value = data['service_data']['value']
        entities = data['service_data']['entity_id']

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
        service = data['service']
        entities = data['service_data']['entity_id']

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
                        self.set_state(entity_id = entity, state = value, attributes={'friendly_name' : item['friendly_name'], 'min' : item['min'], 'max' : item['max'], 'step' : item['step']})
                    elif item['type'] == 'switch':
                        self.set_state(entity_id = entity, state = ('on' if value else 'off'), attributes = {'friendly_name' : item['friendly_name']})
                    elif item['type'] == 'select':
                        self.set_state(entity_id = entity, state = value, attributes = {'friendly_name' : item['friendly_name'], 'options' : item['options']})

    def load_user_config(self):
        """
        Load config from HA
        """

        # Find values and monitor config
        for item in CONFIG_ITEMS:
            name = item['name']
            type = item['type']
            entity = type + "." + "predbat_" + name
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
                except ValueError:
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

    def auto_config(self):
        """
        Auto configure
        match arguments with sensors
        """

        states = self.get_state()
        state_keys = states.keys()
        disabled = []

        # Find each arg re to match
        for arg in self.args:
            arg_value = self.args[arg]
            if isinstance(arg_value, str) and arg_value.startswith('re:'):
                my_re = '^' + arg_value[3:] + '$'
                matched = False
                for key in state_keys:
                    res = re.search(my_re, key)
                    if res:
                        if len(res.groups()) > 0:
                            self.log('Regular expression argument {} matched {} with {}'.format(arg, my_re, res.group(1)))
                            self.args[arg] = res.group(1)
                            matched = True
                            break
                        else:
                            self.log('Regular expression argument {} Matched {} with {}'.format(arg, my_re, res.group(0)))
                            self.args[arg] = res.group(0)
                            matched = True
                            break
                if not matched:
                    self.log("WARN: Regular expression argument: {} unable to match {}, now will disable".format(arg, arg_value))
                    disabled.append(arg)

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
        self.reset()
        self.auto_config()
        if self.args.get('user_config_enable', False):
            self.load_user_config()
        
        if SIMULATE and SIMULATE_LENGTH:
            # run once to get data
            SIMULATE = False
            self.update_pred()
            soc_best = self.predict_soc_best.copy()
            self.log("Best SOC array {}".format(soc_best))
            SIMULATE = True

            now = datetime.now()
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            minutes_now = int((now - midnight).seconds / 60)

            for offset in range (0, SIMULATE_LENGTH, 30):
                self.simulate_offset = offset + 30 - (minutes_now % 30)
                self.sim_soc_kw = soc_best[int(self.simulate_offset / 5) * 5]
                self.log(">>>>>>>>>> Simulated offset {} soc {} <<<<<<<<<<<<".format(self.simulate_offset, self.sim_soc_kw))
                self.update_pred()
        else:
            # Run every N minutes aligned to the minute
            now = datetime.now()
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            seconds_now = (now - midnight).seconds
            run_every = self.get_arg('run_every', 5) * 60

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
                self.run_every(self.update_time_loop, now, 15, random_start=0, random_end=0)

    def update_time_loop(self, cb_args):
        """
        Called every 15 seconds
        """
        if self.update_pending and not self.prediction_started:
            self.prediction_started = True
            self.update_pending = False
            try:
                self.update_pred()
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
                self.update_pred()
            finally:
                self.prediction_started = False
            self.prediction_started = False
 