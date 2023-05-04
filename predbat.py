import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timezone, timedelta
from tzlocal import get_localzone
import math

#
# Battery Prediction app
#  
# Note - tzlocal must be added to the system libraries in AppDeamon config
#

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"

class PredBat(hass.Hass):

  def minute_data(self, history, days, now, state_key, last_updated_key, format_seconds, backwards, hourly, to_key=None):
     bk = {}
     newest_state = 100
     newest_age = 99999
     
     if format_seconds:
        format_string = TIME_FORMAT_SECONDS # 2023-04-25T19:33:47.861967+00:00
     else:
        format_string = TIME_FORMAT # 2023-04-25T19:33:47+00:00

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
        
        if to_key:
            to_time = datetime.strptime(b[to_key], format_string)
        else:
            to_time = None
        
        if backwards:
            td = now - last_updated_time
            if (to_time):
                td_to = now - to_time
        else:
            td = last_updated_time - now
            if (to_time):
                td_to = to_time - now
            
        minutes = int(td.seconds / 60) + int(td.days * 60*24)
        if to_time:
            minutes_to = int(td_to.seconds / 60) + int(td_to.days * 60*24)
        
        if minutes < newest_age:
            newest_age = minutes
            newest_state = state
        
        if to_time:
            minute = minutes
            while (minute < minutes_to):
                bk[minute] = state
                minute += 1
        else:
            bk[minutes] = state
    
     # If we only have a start time then fill the gaps with the last values
     if not to_key:
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
     
     forecast_minutes = self.forecast_hours * 60
     predict_soc = {}
     predict_soc_time = {}
     minute = 0
     minute_left = forecast_minutes
     soc = self.soc_kw
     export_kwh = 0
     import_kwh = 0
     import_kwh_house = 0
     import_kwh_battery = 0
     metric = 0
     
     # For the SOC calculation we need to stop at the second charge window to avoid confusing multiple days out 
     end_record = min(forecast_minutes, self.charge_start_time_minutes + 24*60 - self.minutes_now)
     record = True
     
     # self.log("Minutes since yesterday " + str(self.difference_minutes) + " load past day " + str(load_yesterday) + " load past day now " + str(load_yesterday_now) + " end record " + str(end_record))
     
     # Simulate each forward minute
     while minute < forecast_minutes:
         
        # Outside the recording window?
        if minute >= end_record:
            record = False
            
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
            if self.debug_enable and minute % 60 == 0:
                self.log("Hour %s car charging hold" % (minute/60))
                
        # Are we within the charging time window?
        if self.charge_enable and soc < charge_limit and (
                            (minute_absolute >= self.charge_start_time_minutes and minute_absolute < self.charge_end_time_minutes) or
                            (minute_absolute >= self.charge_start_time_minutes + 24*60 and minute_absolute < self.charge_end_time_minutes + 24*60) or
                            (minute_absolute >= self.charge_start_time_minutes + 24*60*2 and minute_absolute < self.charge_end_time_minutes + 24*60*2)
                            ):
            old_soc = soc
            soc = min(soc + self.charge_rate, charge_limit)
            
            # Apply battery loss to computed charging energy
            # For now we ignore PV in this as it's probably not a major factor when mains charging is enabled
            if record:
               energy = max(0, soc - old_soc - pv_now) / self.battery_loss
               import_kwh += energy
               import_kwh_battery += energy
               if minute_absolute in self.octopus_import:
                   metric += self.octopus_import[minute_absolute] * energy
               else:
                   metric += self.metric_battery * energy
            
            if self.debug_enable and minute % 60 == 0:
                self.log("Hour %s battery charging target soc %s" % (minute/60, charge_limit))
        else:
            diff = load_yesterday - pv_now
            
            # Apply battery loss to charging from PV
            if diff < 0:
                diff *= self.battery_loss
                
            if diff > self.discharge_rate:
                soc -= self.discharge_rate
                if record:
                   energy = (diff - self.discharge_rate)
                   import_kwh += energy
                   import_kwh_house += energy
                   if minute_absolute in self.octopus_import:
                       metric += self.octopus_import[minute_absolute] * energy
                   else:
                       metric += self.metric_house * energy
            else:
                soc -= diff
                
        if soc < self.reserve:
            if record:
               energy = self.reserve - soc
               import_kwh += energy 
               import_kwh_house += energy
               if minute_absolute in self.octopus_import:
                   metric += self.octopus_import[minute_absolute] * energy
               else:
                   metric += self.metric_house * energy
            soc = self.reserve
            
        if soc > self.soc_max:
            if record:
               energy = soc - self.soc_max
               export_kwh += energy
               if minute_absolute in self.octopus_export:
                   metric -= self.octopus_export[minute_absolute] * energy
               else:
                   metric -= self.metric_export * energy
            soc = self.soc_max
        
        if self.debug_enable and minute % 60 == 0:
            self.log("Hour %s load_yesterday %s pv_now %s soc %s" % (minute/60, load_yesterday, pv_now, soc))
        
        predict_soc[minute] = self.dp2(soc)
        
        # Only store every 10 minutes for data-set size
        if minute % 10 == 0:
            predict_soc_time[minute_timestamp.strftime(TIME_FORMAT)] = self.dp2(soc)
        
        # Store the number of minutes until the battery runs out
        if record and soc <= self.reserve:
            if minute_left > minute:
                minute_left = minute
                
        # Record final soc
        if record:
           final_soc = soc
        
        minute += 1
        
     #self.log("load yesterday " + str(load_minutes))
     self.log("predict soc %s metric %s" % (final_soc, metric))

     hours_left = minute_left / 60.0
     charge_limit_percent = min(int((float(charge_limit) / self.soc_max * 100.0) + 0.5), 100)

     # Compute metric (cost) for this simulation
     #metric = (import_kwh_house * self.metric_house) + (import_kwh_battery * self.metric_battery) - (export_kwh * self.metric_export)
     
     if save:
        self.set_state("predbat.battery_hours_left", state=self.dp2(hours_left), attributes = {'friendly_name' : 'Battery Hours left', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'step' : 0.5})
        self.set_state("predbat.soc_kw", state=self.dp2(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
        self.set_state("predbat.export_energy", state=self.dp2(export_kwh), attributes = {'friendly_name' : 'Predicted exports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.import_energy", state=self.dp2(import_kwh), attributes = {'friendly_name' : 'Predicted imports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.import_energy_battery", state=self.dp2(import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.import_energy_house", state=self.dp2(import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.log("Battery has " + str(hours_left) + " hours left - now at " + str(self.soc_kw))
        self.set_state("predbat.metric", state=self.dp2(metric), attributes = {'friendly_name' : 'Predicted metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
        self.set_state("predbat.duration", state=self.dp2(end_record/60), attributes = {'friendly_name' : 'Predicted duration', 'state_class': 'measurement', 'unit_of_measurement': 'hours'})
        
     if save_best:
        self.log('Saving best data with charge_limit %s' % charge_limit)
        self.set_state("predbat.soc_kw_best", state=self.dp2(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
        self.set_state("predbat.best_charge_limit_kw", state=self.dp2(charge_limit), attributes = {'friendly_name' : 'Predicted charge limit kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_charge_limit", state=charge_limit_percent, attributes = {'friendly_name' : 'Predicted charge limit best', 'state_class': 'measurement', 'unit_of_measurement': '%'})
        self.set_state("predbat.best_export_energy", state=self.dp2(export_kwh), attributes = {'friendly_name' : 'Predicted exports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_import_energy", state=self.dp2(import_kwh), attributes = {'friendly_name' : 'Predicted imports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_import_energy_battery", state=self.dp2(import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_import_energy_house", state=self.dp2(import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_metric", state=self.dp2(metric), attributes = {'friendly_name' : 'Predicted best metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
         
     return metric, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh

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

  def rate_replicate(self, rates):
     # We don't get enough hours of data for Octopus, so lets assume it repeats until told others
     forecast_minutes = self.forecast_hours * 60
     minute = 0
     while minute < forecast_minutes:
         if minute not in rates:
             minute_mod = minute % (24*60)
             rates[minute] = rates[minute_mod]
         minute += 1
     return rates
     
  def rate_scan(self, rates):
     forecast_minutes = self.forecast_hours * 60
     rate_min = 99999
     rate_min_minute = 0
     rate_max_minute = 0
     rate_max = 0
     rate_average = 0
     rate_n = 0
     
     rate_low_start = -1
     rate_low_end = -1
     rate_low_rate = 99999
     rate_low_threshold = 0.8
     rate_low_average = 0
     rate_low_count = 0
     
     self.log("Scan Octopus rates %s", rates)
     minute = self.minutes_now
     while minute < forecast_minutes:
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
     rate_average /= rate_n
     self.log("Rates min %s max %s average %s" % (rate_min, rate_max, rate_average))
     
     # Find low rate period (below average)
     minute = self.minutes_now
     while minute < forecast_minutes:
         
         # Don't allow starts beyond 24-hours
         if minute >= 24*60 and rate_low_start < 0:
             break
         
         if minute in rates:
             rate = rates[minute]
             if rate <= (rate_average * rate_low_threshold):
                if rate_low_start < 0:
                    rate_low_start = minute
                    rate_low_end = forecast_minutes - 1
                    rate_low_count = 0
                if rate_low_end > minute:
                   rate_low_average += rate
                   rate_low_count += 1
             else:
                if rate_low_start >= 0 and rate_low_end >= minute:
                    rate_low_end = minute
                    break
         else:
             if rate_low_start >= 0 and rate_low_end >= minute:
                 rate_low_end = minute
             break
         minute += 1
         
     rate_low_average = self.dp2(rate_low_average / rate_low_count)
     if (rate_low_start >= 0):
         self.log("Low rate period next is %s-%s @%s !" % (rate_low_start / 60, rate_low_end / 60, rate_low_average))
         
         rate_low_start_date = self.midnight_utc + timedelta(minutes=rate_low_start)
         rate_low_end_date = self.midnight_utc + timedelta(minutes=rate_low_end)
         
         time_format_time = '%H:%M:%S'

         self.set_state("predbat.low_rate_start", state=rate_low_start_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next low rate start', 'state_class': 'timestamp'})
         self.set_state("predbat.low_rate_end", state=rate_low_end_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next low rate end', 'state_class': 'timestamp'})
         self.set_state("predbat.low_rate_cost", state=rate_low_average, attributes = {'friendly_name' : 'Next low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
     else:
         self.log("No low rate period found")
         self.set_state("predbat.low_rate_start", state='undefined', attributes = {'friendly_name' : 'Next low rate start', 'device_class': 'timestamp'})
         self.set_state("predbat.low_rate_end", state='undefined', attributes = {'friendly_name' : 'Next low rate end', 'device_class': 'timestamp'})
         self.set_state("predbat.low_rate_cost", state=rate_average, attributes = {'friendly_name' : 'Next low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
     
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
     self.forecast_days = int((self.forecast_hours + 23)/24)
     
     load_minutes = self.minute_data(self.get_history(entity_id = self.args['load_today'], days = self.days_previous + 1)[0], self.days_previous + 1, now_utc, 'state', 'last_updated', True, True, False)
     self.soc_kw = float(self.get_state(entity_id = self.args['soc_kw'], default=0))
     self.soc_max = float(self.get_state(entity_id = self.args['soc_max'], default=0))
     reserve_percent = float(self.get_state(entity_id = self.args['reserve'], default=0))
     self.metric_house = self.args.get('metric_house', 38.0)
     self.metric_battery = self.args.get('metric_battery', 7.5)
     self.metric_export = self.args.get('metric_export', 4)
     self.metric_min_improvement = self.args.get('metric_min_improvement', 5)
     self.octopus_import = {}
     self.octopus_export = {}
     if 'metric_octopus_import' in self.args:
         data_import = self.get_state(entity_id = self.args['metric_octopus_import'], attribute='rates')
         self.octopus_import = self.rate_replicate(self.minute_data(data_import, self.forecast_days, self.midnight_utc, 'rate', 'from', False, False, False, to_key='to'))
         self.rate_scan(self.octopus_import)
     else:
         self.log("No Octopus rate data provided - using default metric")
     if 'metric_octopus_import' in self.args:
         data_export = self.get_state(entity_id = self.args['metric_octopus_export'], attribute='rates')
         self.octopus_export = self.rate_replicate(self.minute_data(data_export, self.forecast_days, self.midnight_utc, 'rate', 'from', False, False, False, to_key='to'))
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
    
     # Fetch PV forecast if enbled, today must be enabled, other days are optional
     if 'pv_forecast_today' in self.args:
        pv_forecast_data    = self.get_state(entity_id = self.args['pv_forecast_today'], attribute='forecast')
        if 'pv_forecast_tomorrow' in self.args:
            pv_forecast_data += self.get_state(entity_id = self.args['pv_forecast_tomorrow'], attribute='forecast')
        if 'pv_forecast_d3' in self.args:
            pv_forecast_data += self.get_state(entity_id = self.args['pv_forecast_d3'], attribute='forecast')
        if 'pv_forecast_d4' in self.args:
            pv_forecast_data += self.get_state(entity_id = self.args['pv_forecast_d4'], attribute='forecast')
        if 'pv_forecast_d5' in self.args:
            pv_forecast_data += self.get_state(entity_id = self.args['pv_forecast_d5'], attribute='forecast')
        if 'pv_forecast_d6' in self.args:
            pv_forecast_data += self.get_state(entity_id = self.args['pv_forecast_d6'], attribute='forecast')
        if 'pv_forecast_d7' in self.args:
            pv_forecast_data += self.get_state(entity_id = self.args['pv_forecast_d7'], attribute='forecast')
        pv_forecast_minute = self.minute_data(pv_forecast_data, self.forecast_days, self.midnight_utc, 'pv_estimate', 'period_start', False, False, True)
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
            was_debug = self.debug_enable
            self.debug_enable = False
            metric, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh = self.run_prediction(now, try_soc, load_minutes, pv_forecast_minute, False, False)
            self.debug_true = was_debug
            if self.debug_enable:
                self.log("Trying soc %s gives import battery %s house %s export %s metric %s" % 
                        (try_soc, import_kwh_battery, import_kwh_house, export_kwh, metric))
            
            # Only select the lower SOC if it makes a notable improvement has defined by min_improvement
            if metric + self.metric_min_improvement < best_metric:
                best_metric = metric
                best_soc = try_soc
            try_soc -= 0.5
         
        # Simulate best - add margin first and clamp to min and then clamp to max
        # Also save the final selected metric
        best_soc = best_soc + self.best_soc_margin
        best_soc = max(self.best_soc_min, best_soc)
        best_soc = min(best_soc, self.soc_max)
        self.log("Best soc calculated at %s (margin added %s and min %s) with metric %s" % (best_soc, self.best_soc_margin, self.best_soc_min, best_metric))
        best_metric, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh = self.run_prediction(now, best_soc, load_minutes, pv_forecast_minute, False, True)
        self.log("Best soc %s gives import battery %s house %s export %s metric %s" % 
                (best_soc, import_kwh_battery, import_kwh_house, export_kwh, metric))
        
        # Set the SOC, only do it within the window before the charge starts
        if self.args.get('set_soc_enable', False) and self.charge_enable:
            if (self.minutes_now < self.charge_start_time_minutes) and (self.charge_start_time_minutes - self.minutes_now) <= self.set_soc_minutes:
                self.adjust_battery_target(charge_limit_percent)
            else:
                self.log("Not setting charging SOC as we are not within the window (now %s target set_soc_minutes %s charge start time %s" % (self.minutes_now,self.set_soc_minutes, self.charge_start_time_minutes))
     
     # Simulate current settings
     metric, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh = self.run_prediction(now, self.charge_limit, load_minutes, pv_forecast_minute, True, False)
     self.log("Completed run")
     
  def initialize(self):
     self.log("Startup")
     #self.update_pred()
     # Run every 5 minutes
     self.run_every(self.run_time_loop, "now", self.args.get('run_every', 5) * 60)
     
  def run_time_loop(self, cb_args):
     self.update_pred()
  