import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timezone, timedelta
from tzlocal import get_localzone
import math

#
# Battery Prediction app
#  
# Note - tzlocal must be added to the system libraries in AppDeamon config
#

class PredBat(hass.Hass):

  def minute_data(self, history, days, now, state_key, last_updated_key, format_seconds, backwards, hourly):
     bk = {}
     newest_state = 100
     newest_age = 99999
     
     if format_seconds:
        format_string = "%Y-%m-%dT%H:%M:%S.%f%z" # 2023-04-25T19:33:47.861967+00:00
     else:
        format_string = "%Y-%m-%dT%H:%M:%S%z" # 2023-04-25T19:33:47+00:00

     for b in history:
        if state_key not in b:
            continue
        if b[state_key] == 'unavailable' or b[state_key] == 'unknown':
            continue
        state = float(b[state_key])
        if hourly:
            state /= 60
        last_updated = b[last_updated_key]
        last_updated_time = datetime.strptime(last_updated, format_string)
        
        if backwards:
            td = now - last_updated_time
        else:
            td = last_updated_time - now
            
        minutes = int(td.seconds / 60) + int(td.days * 60*24)
        if minutes < newest_age:
            newest_age = minutes
            newest_state = state
        bk[minutes] = state
     minute = 0
     state = newest_state
     while minute < 60 * 24 * days:
         if minute in bk:
             state = bk[minute]
         else:
             bk[minute] = state
         minute += 1
     return bk
      
  def minutes_since_yesterday(self, now):
     # Calculate the date and time for 23:59 yesterday
     yesterday = now - timedelta(days=1)
     yesterday_at_2359 = datetime.combine(yesterday, datetime.max.time())
     difference = now - yesterday_at_2359
     difference_minutes = int((difference.seconds + 59) / 60)
     return difference_minutes
     
  def dp2(self, value):
     return math.ceil(value*100)/100

  def run_prediction(self, now, charge_limit, load_minutes, pv_forecast_minute, save, save_best):
      
     six_days = 24*60*(self.days_previous - 1)
     
     # Offset by 6 (configurable) days to get to last week
     load_yesterday = load_minutes[self.difference_minutes + six_days]
     load_yesterday_now = load_minutes[24*60 + six_days]
     self.log("Minutes since yesterday " + str(self.difference_minutes) + " load past day " + str(load_yesterday) + " load past day now " + str(load_yesterday_now))
     
     forecast_minutes = min(self.forecast_hours * 60, self.minutes_to_midnight + 24*60)
     
     predict_soc = {}
     predict_soc_time = {}
     minute = 0
     minute_left = forecast_minutes
     soc = self.soc_kw
     export_kwh = 0
     import_kwh = 0
     import_kwh_house = 0
     import_kwh_battery = 0
     
     # Simulate each forward minute
     while minute < forecast_minutes:
         
        minute_yesterday = 24 * 60 - minute + six_days
        # Average previous load over 10 minutes due to sampling accuracy
        load_yesterday = (load_minutes[minute_yesterday] - load_minutes[minute_yesterday + 10]) / 10.0
        
        # Resets at midnight so avoid wrap
        if load_yesterday < 0:
            load_yesterday = load_minutes[minute_yesterday]
            
        minute_absolute = minute + self.minutes_now
        minute_timestamp = self.midnight_utc + timedelta(seconds=60*minute_absolute)
        
        pv_now = pv_forecast_minute.get(minute_absolute, 0.0)

        if self.car_charging_hold and (load_yesterday >= self.car_charging_threshold):
            # Car charging hold - ignore car charging in computation
            load_yesterday = 0
            if self.debug_enable and minute % 15 == 0:
                self.log("Hour %s car charging hold" % (minute/60))
                
        # Are we within the charging time window?
        if self.charge_enable and soc < charge_limit and (
                            (minute_absolute >= self.charge_start_time_minutes and minute_absolute < self.charge_end_time_minutes) or
                            (minute_absolute >= self.charge_start_time_minutes + 24*60 and minute_absolute < self.charge_end_time_minutes + 24*60)
                            ):
            old_soc = soc
            soc = min(soc + self.charge_rate, charge_limit)
            
            # Apply battery loss to computed charging energy
            # For now we ignore PV in this as it's probably not a major factor when mains charging is enabled
            import_kwh += max(0, soc - old_soc - pv_now) / self.battery_loss
            import_kwh_battery += max(0, soc - old_soc - pv_now) / self.battery_loss
            
            if self.debug_enable and minute % 15 == 0:
                self.log("Hour %s battery charging target soc %s" % (minute/60, charge_limit))
        else:
            diff = load_yesterday - pv_now
            
            # Apply battery loss to charging from PV
            if diff < 0:
                diff *= self.battery_loss
                
            if diff > self.discharge_rate:
                soc -= self.discharge_rate
                import_kwh += (diff - self.discharge_rate)
                import_kwh_house += (diff - self.discharge_rate)
            else:
                soc -= diff
                
        if soc < self.reserve:
            import_kwh += self.reserve - soc 
            import_kwh_house += self.reserve - soc
            soc = self.reserve
            
        if soc > self.soc_max:
            export_kwh += soc - self.soc_max
            soc = self.soc_max
        
        if self.debug_enable and minute % 15 == 0:
            self.log("Hour %s load_yesterday %s pv_now %s soc %s" % (minute/60, load_yesterday, pv_now, soc))
        
        predict_soc[minute] = self.dp2(soc)
        
        # Only store every 5 minutes for data-set size
        if minute % 5 == 0:
            predict_soc_time[str(minute_timestamp)] = self.dp2(soc)
        
        # Store the worst caste
        if soc <= self.reserve:
            if minute_left > minute:
                minute_left = minute
        minute += 1
        
     #self.log("load yesterday " + str(load_minutes))
     #self.log("predict soc " + str(predict_soc_time))

     hours_left = minute_left / 60.0
     charge_limit_percent = min(int((float(charge_limit) / self.soc_max * 100.0) + 0.5), 100)

     if save:
        self.set_state("predbat.battery_hours_left", state=self.dp2(hours_left), attributes = {'friendly_name' : 'Battery Hours left', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'step' : 0.5})
        self.set_state("predbat.soc_kw", state=self.dp2(soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
        self.set_state("predbat.export_energy", state=self.dp2(export_kwh), attributes = {'friendly_name' : 'Predicted exports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.import_energy", state=self.dp2(import_kwh), attributes = {'friendly_name' : 'Predicted imports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.import_energy_battery", state=self.dp2(import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.import_energy_house", state=self.dp2(import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.log("Battery has " + str(hours_left) + " hours left - now at " + str(self.soc_kw))
        
     if save_best:
        self.log('Saving best data with charge_limit %s' % charge_limit)
        self.set_state("predbat.soc_kw_best", state=self.dp2(soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
        self.set_state("predbat.best_charge_limit_kw", state=self.dp2(charge_limit), attributes = {'friendly_name' : 'Predicted charge limit kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_charge_limit", state=charge_limit_percent, attributes = {'friendly_name' : 'Predicted charge limit best', 'state_class': 'measurement', 'unit_of_measurement': '%'})
        self.set_state("predbat.best_export_energy", state=self.dp2(export_kwh), attributes = {'friendly_name' : 'Predicted exports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_import_energy", state=self.dp2(import_kwh), attributes = {'friendly_name' : 'Predicted imports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_import_energy_battery", state=self.dp2(import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_import_energy_house", state=self.dp2(import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
         
     return charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh

  def adjust_battery_target(self, soc):
     # Check current setting and adjust
     current_soc = float(self.get_state(entity_id = self.args['soc_percent'], default=100))
     if current_soc != soc:
        self.log("Current SOC is %s and new target is %s" % (current_soc, soc))
        entity_soc = self.get_entity(self.args['soc_percent'])
        if entity_soc:
            entity_soc.call_service("set_value", value=soc)
            if self.args.get('set_soc_notify', False):
                self.call_service("notify/notify", message='Predbat: Target SOC has been changed to %s' % soc)
        else:
            self.log("WARN: Unable to get entity to set SOC target")
     else:
        self.log("Current SOC is %s already at target" % (current_soc))
        
  def update_pred(self):
     local_tz = get_localzone()      
     now_utc = datetime.now(local_tz) #timezone.utc)
     now = datetime.now()
     self.log("PredBat - update at: " + str(now_utc))
     
     self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
     self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
     
     self.difference_minutes = self.minutes_since_yesterday(now)
     self.minutes_now = int((now - self.midnight).seconds / 60)
     self.minutes_to_midnight = 24*60 - self.minutes_now
     
     self.days_previous = self.args.get('days_previous', 7)
     self.forecast_hours = self.args.get('forecast_hours', 24)
     
     load_minutes = self.minute_data(self.get_history(entity_id = self.args['load_today'], days = self.days_previous + 1)[0], self.days_previous + 1, now_utc, 'state', 'last_updated', True, True, False)
     self.soc_kw = float(self.get_state(entity_id = self.args['soc_kw'], default=0))
     self.soc_max = float(self.get_state(entity_id = self.args['soc_max'], default=0))
     reserve_percent = float(self.get_state(entity_id = self.args['reserve'], default=0))
     metric_house = self.args.get('metric_house', 38.0)
     metric_battery = self.args.get('metric_battery', 7.5)
     metric_export = self.args.get('metric_export', 4)
     self.reserve = self.soc_max * reserve_percent / 100.0
     self.battery_loss = 1.0 - self.args.get('battery_loss', 0.05)
     self.best_soc_margin = self.args.get('best_soc_margin', 0.5)
     self.best_soc_min = self.args.get('best_soc_min', 4)
     self.set_soc_minutes = self.args.get('set_soc_minutes', 24*60)
     
     self.charge_enable = self.get_state(self.args['charge_enable'], default = False)
     if self.charge_enable:
        charge_start_time = datetime.strptime(self.get_state(self.args['charge_start_time']), "%H:%M:%S")
        charge_end_time = datetime.strptime(self.get_state(self.args['charge_end_time']), "%H:%M:%S")
        
        self.charge_start_time_minutes = charge_start_time.hour * 60 + charge_start_time.minute
        self.charge_end_time_minutes = charge_end_time.hour * 60 + charge_end_time.minute
        
        if self.charge_end_time_minutes < self.charge_start_time_minutes:
            self.charge_end_time_minutes += 60 * 24
            
        self.charge_limit = float(self.get_state(self.args['charge_limit'])) * self.soc_max / 100.0
        self.charge_rate = float(self.get_state(self.args['charge_rate'])) / 1000.0 / 60.0
        self.log("Charge settings are: %s-%s limit %s power %s (per minute)" % (str(self.charge_start_time_minutes), str(self.charge_end_time_minutes), str(self.charge_limit), str(self.charge_rate)))
        
     # battery max discharge rate
     self.discharge_rate = float(self.get_state(self.args['discharge_rate'])) / 1000.0 / 60.0
    
     if 'pv_forecast_today' in self.args:
        pv_forecast_data_today    = self.get_state(entity_id = self.args['pv_forecast_today'], attribute='forecast')
        pv_forecast_data_tomorrow = self.get_state(entity_id = self.args['pv_forecast_tomorrow'], attribute='forecast')
        pv_forecast_minute = self.minute_data(pv_forecast_data_today + pv_forecast_data_tomorrow, 24 + self.forecast_hours, self.midnight_utc, 'pv_estimate', 'period_start', False, False, True)
     else:
        pv_forecast_minute = {}

     # Car charging hold - when enabled battery is held during car charging in simulation
     self.car_charging_hold = self.args.get('car_charging_hold', False)
     self.car_charging_threshold = float(self.args.get('car_charging_threshold', 6.0)) / 60.0
     self.debug_enable = self.args.get('debug_enable', False)
     self.log("Car charging hold %s threshold %s" % (self.car_charging_hold, self.car_charging_threshold*60.0))

     # Try different battery SOCs to get the best result
     if self.args.get('calculate_best', False):
        try_soc = self.soc_max
        best_soc = try_soc
        best_metric = 999999
        while try_soc > self.reserve:
            charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh = self.run_prediction(now, try_soc, load_minutes, pv_forecast_minute, False, False)
            metric = import_kwh_house * metric_house + import_kwh_battery * metric_battery - export_kwh * metric_export
            if self.debug_enable:
                self.log("Trying soc %s gives import battery %s house %s export %s metric %s" % (try_soc, import_kwh_battery, import_kwh_house, export_kwh, metric))
            if metric < best_metric:
                best_metric = metric
                best_soc = try_soc
            try_soc -= 0.5
         
        # Simulate best - add margin first and clamp to min and then clamp to max
        best_soc = best_soc + self.best_soc_margin
        best_soc = max(self.best_soc_min, best_soc)
        best_soc = min(best_soc, self.soc_max)
        self.log("Best soc calculated at %s (margin added %s and min %s) with metric %s" % (best_soc, self.best_soc_margin, self.best_soc_min, best_metric))
        charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh = self.run_prediction(now, best_soc, load_minutes, pv_forecast_minute, False, True)
        
        # Set the SOC, only do it within the window before the charge starts
        if self.args.get('set_soc_enable', False) and self.charge_enable:
            if (self.minutes_now < self.charge_start_time_minutes) and (self.charge_start_time_minutes - self.minutes_now) <= self.set_soc_minutes:
                self.adjust_battery_target(charge_limit_percent)
            else:
                self.log("Not setting charging SOC as we are not within the window (now %s target set_soc_minutes %s charge start time %s" % (self.minutes_now,self.set_soc_minutes, self.charge_start_time_minutes))
     
     # Simulate current settings
     charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh = self.run_prediction(now, self.charge_limit, load_minutes, pv_forecast_minute, True, False)
     
  def initialize(self):
     self.log("Startup")
     # Run every 5 minutes
     self.run_every(self.run_time_loop, "now", 5 * 60)
     
  def run_time_loop(self, cb_args):
     self.update_pred()
  