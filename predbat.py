import appdaemon.plugins.hass.hassapi as hass
from datetime import datetime, timezone, timedelta
import pytz
import math

#
# Battery Prediction app
#
#

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_OCTOPUS = "%Y-%m-%d %H:%M:%S%z"

class PredBat(hass.Hass):

  def mintes_to_time(self, updated, now):
     td = updated - now
     minutes = int(td.seconds / 60) + int(td.days * 60*24)
     return minutes
     
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
        bk2 = {}
        state = newest_state
        for minute in range(0, 60*24*days):
            if minute in bk:
                state = bk[minute]
            bk2[minute] = state
            minute += 1
        return bk2
     else:
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

  def in_charge_window(self, minute):
     for window in self.charge_window:
         if minute >= window['start'] and minute < window['end']:
             return True
     return False
 
  def clean_incrementing_reverse(self, data):
    
     new_data = {}
     length = len(data)
     
     increment = 0
     last = data[length - 1]
     
     for index in range(0, length - 1):
        rindex = length - index - 1
        nxt = data[rindex]
        if nxt >= last:
            increment += nxt - last
        last = nxt
        new_data[rindex] = increment
        
     return new_data
            
        
  def get_from_incrementing(self, data, index):
     
     offset = 10
     
     value = data[index]
     old_value = data[index + offset]
     
     diff = (value - old_value) / offset
     
     if diff < 0:
         self.log("WARN: Negative diff %s at %s was %s %s .. %s %s" % (diff, index, data[index], data[index+1], data[index + offset - 1], data[index + offset]))
         diff = 0
         
     return diff
     
        
     
  def run_prediction(self, now, charge_limit, load_minutes, pv_forecast_minute, save, save_best):
      
     six_days = 24*60*(self.days_previous - 1)
     
     predict_soc = {}
     predict_soc_time = {}
     minute = 0
     minute_left = self.forecast_minutes
     soc = self.soc_kw
     soc_min = self.soc_max
     charge_has_run = False
     charge_has_started = False
     export_kwh = 0
     import_kwh = 0
     import_kwh_house = 0
     import_kwh_battery = 0
     load_kwh = 0
     metric = 0
     metric_time = {}
     load_kwh_time = {}
     
     # For the SOC calculation we need to stop at the second charge window to avoid confusing multiple days out 
     end_record = self.forecast_minutes
     if len(self.charge_window) > 1:
         end_record = min(end_record, self.charge_window[1]['start'] - self.minutes_now)
     record = True

     # Simulate each forward minute
     while minute < self.forecast_minutes:
         
        minute_yesterday = 24 * 60 - minute + six_days
        # Average previous load over 10 minutes due to sampling accuracy
        load_yesterday = self.get_from_incrementing(load_minutes, minute_yesterday)
        
        minute_absolute = minute + self.minutes_now
        minute_timestamp = self.midnight_utc + timedelta(seconds=60*minute_absolute)
        
        # Outside the recording window?
        if minute >= end_record and record:
            record = False

        pv_now = pv_forecast_minute.get(minute_absolute, 0.0)

        # Car charging hold
        if self.car_charging_hold and self.car_charging_energy:
            # Hold based on data
            car_energy = self.get_from_incrementing(self.car_charging_energy, minute_yesterday)
            if self.debug_enable and car_energy > 0.0 and (minute % 60) == 0 and (minute < 60*48):
                self.log("Hour %s car charging hold with data %s load now %s metric %s" % (minute/60, car_energy, load_yesterday, metric))
            load_yesterday = max(0, load_yesterday - car_energy)
        elif self.car_charging_hold and (load_yesterday >= self.car_charging_threshold):
            # Car charging hold - ignore car charging in computation based on threshold
            load_yesterday = 0
            if self.debug_enable and minute % 60 == 0:
                self.log("Hour %s car charging hold" % (minute/60))
                
        # Count load
        if record:
           load_kwh += load_yesterday
                
        # Are we within the charging time window?
        if self.charge_enable and soc < charge_limit and self.in_charge_window(minute_absolute):
            old_soc = soc
            soc = min(soc + self.charge_rate, charge_limit)
            
            # Apply battery loss to computed charging energy
            # For now we ignore PV in this as it's probably not a major factor when mains charging is enabled
            if record:
               energy = max(0, soc - old_soc - pv_now) / self.battery_loss
               
               # Must add in grid import for load
               energy += load_yesterday
               import_kwh += energy
               import_kwh_battery += energy
               if minute_absolute in self.rate_import:
                   metric += self.rate_import[minute_absolute] * energy
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
                   if self.charge_enable and self.in_charge_window(minute_absolute):
                       # If the battery is on charge anyhow then imports are kind of the same as battery charging (price wise)
                       import_kwh_battery += energy
                   else:
                       import_kwh_house += energy
                       
                   if minute_absolute in self.rate_import:
                       metric += self.rate_import[minute_absolute] * energy
                   else:
                       if self.charge_enable and self.in_charge_window(minute_absolute):
                           metric += self.metric_battery * energy
                       else:
                           metric += self.metric_house * energy
            else:
                soc -= diff
                
        if soc < self.reserve:
            if record:
               energy = self.reserve - soc
               import_kwh += energy 
               import_kwh_house += energy
               if minute_absolute in self.rate_import:
                   metric += self.rate_import[minute_absolute] * energy
               else:
                   metric += self.metric_house * energy
            soc = self.reserve
            
        if soc > self.soc_max:
            if record:
               energy = soc - self.soc_max
               export_kwh += energy
               if minute_absolute in self.rate_export:
                   metric -= self.rate_export[minute_absolute] * energy
               else:
                   metric -= self.metric_export * energy
            soc = self.soc_max
        
        if self.debug_enable and minute % 60 == 0:
            self.log("Hour %s load_yesterday %s pv_now %s soc %s" % (minute/60, load_yesterday, pv_now, soc))
        
        predict_soc[minute] = self.dp2(soc)
        
        # Only store every 10 minutes for data-set size
        if (minute % 10) == 0:
            stamp = minute_timestamp.strftime(TIME_FORMAT)
            predict_soc_time[stamp] = self.dp2(soc)
            metric_time[stamp] = self.dp2(metric)
            load_kwh_time[stamp] = self.dp2(load_kwh)
        
        # Store the number of minutes until the battery runs out
        if record and soc <= self.reserve:
            if minute_left > minute:
                minute_left = minute
                
        # Record final soc
        if record:
           final_soc = soc
        
        # Have we pasted the charging time
        if self.charge_enable and self.in_charge_window(minute_absolute):
            charge_has_started = True
        if self.charge_enable and charge_has_started and not self.in_charge_window(minute_absolute):
            charge_has_run = True
        
        # Record soc min
        if record and (charge_has_run or not self.charge_enable):
            soc_min = min(soc_min, soc)
            

        minute += 1
        
     hours_left = minute_left / 60.0
     charge_limit_percent = min(int((float(charge_limit) / self.soc_max * 100.0) + 0.5), 100)
     
     self.log("predict charge limit %s (%s kwh) %% final soc %s kwh metric %s p min_soc %s kwh" % (charge_limit_percent, self.dp2(charge_limit), self.dp2(final_soc), self.dp2(metric), self.dp2(soc_min)))

     # Save data to HA state
     if save:
        self.set_state("predbat.battery_hours_left", state=self.dp2(hours_left), attributes = {'friendly_name' : 'Predicted Battery Hours left', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'step' : 0.5})
        self.set_state("predbat.soc_kw", state=self.dp2(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Predicted SOC kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
        self.set_state("predbat.soc_min_kwh", state=self.dp2(soc_min), attributes = {'friendly_name' : 'Predicted minimum SOC', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
        self.set_state("predbat.charge_limit_kw", state=self.dp2(charge_limit), attributes = {'friendly_name' : 'Predicted charge limit kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.charge_limit", state=charge_limit_percent, attributes = {'friendly_name' : 'Predicted charge limit', 'state_class': 'measurement', 'unit_of_measurement': '%'})
        self.set_state("predbat.export_energy", state=self.dp2(export_kwh), attributes = {'friendly_name' : 'Predicted exports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.load_energy", state=self.dp2(load_kwh), attributes = {'results' : load_kwh_time, 'friendly_name' : 'Predicted load', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.import_energy", state=self.dp2(import_kwh), attributes = {'friendly_name' : 'Predicted imports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.import_energy_battery", state=self.dp2(import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.import_energy_house", state=self.dp2(import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.log("Battery has " + str(hours_left) + " hours left - now at " + str(self.soc_kw))
        self.set_state("predbat.metric", state=self.dp2(metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
        self.set_state("predbat.duration", state=self.dp2(end_record/60), attributes = {'friendly_name' : 'Prediction duration', 'state_class': 'measurement', 'unit_of_measurement': 'hours'})
        
     if save_best:
        self.log('Saving best data with charge_limit %s' % self.dp2(charge_limit))
        self.set_state("predbat.best_battery_hours_left", state=self.dp2(hours_left), attributes = {'friendly_name' : 'Predicted Battery Hours left best', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'step' : 0.5})
        self.set_state("predbat.soc_kw_best", state=self.dp2(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
        self.set_state("predbat.best_soc_min_kwh", state=self.dp2(soc_min), attributes = {'friendly_name' : 'Predicted minimum SOC best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
        self.set_state("predbat.best_charge_limit_kw", state=self.dp2(charge_limit), attributes = {'friendly_name' : 'Predicted charge limit kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_charge_limit", state=charge_limit_percent, attributes = {'friendly_name' : 'Predicted charge limit best', 'state_class': 'measurement', 'unit_of_measurement': '%'})
        self.set_state("predbat.best_load_energy", state=self.dp2(load_kwh), attributes = {'friendly_name' : 'Predicted load best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_export_energy", state=self.dp2(export_kwh), attributes = {'friendly_name' : 'Predicted exports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_import_energy", state=self.dp2(import_kwh), attributes = {'friendly_name' : 'Predicted imports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_import_energy_battery", state=self.dp2(import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_import_energy_house", state=self.dp2(import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
        self.set_state("predbat.best_metric", state=self.dp2(metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted best metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
         
     return metric, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min

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
 
  def adjust_charge_window(self, charge_start_time, charge_end_time):
      
     # Change charge window settings
     old_start = self.get_state(self.args['charge_start_time'])
     old_end = self.get_state(self.args['charge_end_time'])
     new_start = charge_start_time.strftime("%H:%M:%S")
     new_end = charge_end_time.strftime("%H:%M:%S")
     if new_start != old_start:
         entity_start = self.get_entity(self.args['charge_start_time'])
         entity_start.call_service("select_option", option=new_start)
     if new_end != old_end:
         entity_end = self.get_entity(self.args['charge_end_time'])
         entity_end.call_service("select_option", option=new_end)
     if new_start != old_start or new_end != old_end:
         if self.args.get('set_soc_notify', False):
             self.call_service("notify/notify", message='Predbat: Charge window change to: %s - %s' % (new_start, new_end))
         self.log("Updated start and end charge window to %s - %s (old %s - %s)" % (new_start, new_end, old_start, old_end))


  def rate_replicate(self, rates):
     # We don't get enough hours of data for Octopus, so lets assume it repeats until told others
     minute = 0
     # Add 12 extra hours to make sure charging period will end
     while minute < (self.forecast_minutes + 12*60):
         if minute not in rates:
             minute_mod = minute % (24*60)
             rates[minute] = rates[minute_mod]
         minute += 1
     return rates
     
  def find_charge_window(self, rates, minute):
     rate_low_start = -1
     rate_low_end = -1
     rate_low_rate = 99999
     rate_low_threshold = self.args.get('rate_low_threshold', 0.8)
     rate_low_average = 0
     rate_low_count = 0
     
     stop_at = self.forecast_minutes + 12*60
     # Scan for lower rate start and end
     while minute < stop_at:
         # Don't allow starts beyond the forecast window
         if minute >= self.forecast_minutes and rate_low_start < 0:
             break
         
         if minute in rates:
             rate = rates[minute]
             if rate <= (self.rate_average * rate_low_threshold):
                if rate_low_start < 0:
                    rate_low_start = minute
                    rate_low_end = stop_at
                    rate_low_count = 0
                if rate_low_end > minute:
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

  def basic_rates(self, info):
     
     rates = {} 
     
     # Default to house value
     for minute in range(0, self.forecast_minutes):
         rates[minute] = self.metric_house
         
     self.log("Adding rate info %s" % info)
     midnight = datetime.strptime('00:00:00', "%H:%M:%S")
     for this_rate in info:
         start = datetime.strptime(this_rate.get('start', "00:00:00"), "%H:%M:%S")
         end = datetime.strptime(this_rate.get('end', "00:00:00"), "%H:%M:%S")
         rate = this_rate.get('rate', self.metric_house)
         start_minutes = max(self.mintes_to_time(start, midnight), 0)
         end_minutes   = min(self.mintes_to_time(end, midnight), self.forecast_minutes)
         
         if end_minutes <= start_minutes:
             end_minutes += 24*60
             
         self.log("Found rate %s %s to %s" % (rate, start_minutes, end_minutes))
         for minute in range(start_minutes, end_minutes):
             rates[minute] = rate
             
     return rates
     
  def rate_scan(self, rates, octopus_slots):
     rate_min = 99999
     rate_min_minute = 0
     rate_max_minute = 0
     rate_max = 0
     rate_average = 0
     rate_n = 0
     rate_low_min_window = 5
     self.low_rates = []
     
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
         
     self.log("Rates min %s max %s average %s" % (rate_min, rate_max, rate_average))
     self.rate_min = rate_min
     self.rate_max = rate_max
     self.rate_average = rate_average
     
     # Add in any planned octopus slots
     if octopus_slots:
         for slot in octopus_slots:
             start = datetime.strptime(slot['startDtUtc'], TIME_FORMAT_OCTOPUS)
             end = datetime.strptime(slot['endDtUtc'], TIME_FORMAT_OCTOPUS)
             start_minutes = max(self.mintes_to_time(start, self.midnight_utc), 0)
             end_minutes   = min(self.mintes_to_time(end, self.midnight_utc), self.forecast_minutes)
             
             self.log("Octopus Intelligent slot at %s-%s minutes assumed price %s" % (start_minutes, end_minutes, self.rate_min))
             for minute in range(start_minutes, end_minutes):
                 rates[minute] = self.rate_min
                 if self.debug_enable and (minute % 30) == 0:
                     self.log("Set min octopus rate for time %s" % minute)
     
     # Find charging window
     minute = self.minutes_now
     while True:
         rate_low_start, rate_low_end, rate_low_average = self.find_charge_window(rates, minute)
         window = {}
         window['start'] = rate_low_start
         window['end'] = rate_low_end
         window['average'] = rate_low_average
     
         if rate_low_start >= 0:
             if (rate_low_end - rate_low_start) >= rate_low_min_window:
                 self.low_rates.append(window)
             minute = rate_low_end
         else:
             break
     
     if (self.low_rates):
         n = 0
         for window in self.low_rates:
             rate_low_start = window['start']
             rate_low_end = window['end']
             rate_low_average = window['average']
        
             self.log("Low rate period %s-%s @%s !" % (rate_low_start, rate_low_end, rate_low_average))
         
             rate_low_start_date = self.midnight_utc + timedelta(minutes=rate_low_start)
             rate_low_end_date = self.midnight_utc + timedelta(minutes=rate_low_end)
         
             time_format_time = '%H:%M:%S'

             if n == 0:
                 self.set_state("predbat.low_rate_start", state=rate_low_start_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next low rate start', 'state_class': 'timestamp'})
                 self.set_state("predbat.low_rate_end", state=rate_low_end_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next low rate end', 'state_class': 'timestamp'})
                 self.set_state("predbat.low_rate_cost", state=rate_low_average, attributes = {'friendly_name' : 'Next low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
             n += 1
     else:
         self.log("No low rate period found")
         self.set_state("predbat.low_rate_start", state='undefined', attributes = {'friendly_name' : 'Next low rate start', 'device_class': 'timestamp'})
         self.set_state("predbat.low_rate_end", state='undefined', attributes = {'friendly_name' : 'Next low rate end', 'device_class': 'timestamp'})
         self.set_state("predbat.low_rate_cost", state=rate_average, attributes = {'friendly_name' : 'Next low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
         
     return rates
     
  def publish_rates(self, rates, export):    
     # Create rates/time every 30 minutes
     rates_time = {}
     for minute in range(0, self.forecast_minutes, 30):
         minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
         stamp = minute_timestamp.strftime(TIME_FORMAT)
         rates_time[stamp] = rates[minute]

     if export:
        self.set_state("predbat.rates_export", state=rates[self.minutes_now], attributes = {'results' : rates_time, 'friendly_name' : 'Export rates', 'state_class' : 'measurement', 'unit_of_measurement': 'p'})
     else:
        self.set_state("predbat.rates", state=rates[self.minutes_now], attributes = {'results' : rates_time, 'friendly_name' : 'Import rates', 'state_class' : 'measurement', 'unit_of_measurement': 'p'})
     return rates

  # Work out energy costs today (approx)
  def today_cost(self, import_today):
     day_cost = 0
     day_energy = 0
     day_cost_time = {}
     
     for minute in range(0, self.minutes_now):
        minute_back = self.minutes_now - minute
        energy = self.get_from_incrementing(import_today, minute_back)
        day_energy += energy
        day_cost += self.rate_import[minute] * energy
        
        if (minute % 10) == 0:
           minute_timestamp = self.midnight_utc + timedelta(minutes=minute)
           stamp = minute_timestamp.strftime(TIME_FORMAT)
           day_cost_time[stamp] = day_cost
        
     self.set_state("predbat.cost_today", state=day_cost, attributes = {'results' : day_cost_time, 'friendly_name' : 'Cost so far today', 'state_class' : 'measurement', 'unit_of_measurement': 'p'})
     self.log("Todays energy %s cost %s" % (day_energy, day_cost)) 
     return

  def update_pred(self):
     local_tz = pytz.timezone(self.args.get('timezone', "Europe/London"))
     now_utc = datetime.now(local_tz) #timezone.utc)
     now = datetime.now()
     self.log("PredBat - update at: " + str(now_utc))
     
     self.debug_enable = self.args.get('debug_enable', False)
     self.log("Debug enable is %s" % self.debug_enable)
     
     self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
     self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
     
     self.difference_minutes = self.minutes_since_yesterday(now)
     self.minutes_now = int((now - self.midnight).seconds / 60)
     self.minutes_to_midnight = 24*60 - self.minutes_now
     
     self.days_previous = self.args.get('days_previous', 7)
     self.forecast_hours = self.args.get('forecast_hours', 24)
     self.forecast_days = int((self.forecast_hours + 23)/24)
     self.forecast_minutes = self.forecast_hours * 60
     
     load_minutes = self.minute_data(self.get_history(entity_id = self.args['load_today'], days = self.days_previous + 1)[0], self.days_previous + 1, now_utc, 'state', 'last_updated', True, True, False)
     load_minutes = self.clean_incrementing_reverse(load_minutes)
     
     self.soc_kw = float(self.get_state(entity_id = self.args['soc_kw'], default=0))
     self.soc_max = float(self.get_state(entity_id = self.args['soc_max'], default=0))
     reserve_percent = float(self.get_state(entity_id = self.args['reserve'], default=0))
     self.metric_house = self.args.get('metric_house', 38.0)
     self.metric_battery = self.args.get('metric_battery', 7.5)
     self.metric_export = self.args.get('metric_export', 4)
     self.metric_min_improvement = self.args.get('metric_min_improvement', 5)
     self.rate_import = {}
     self.rate_export = {}
     self.rate_slots = []
     self.low_rates = []
     
     # Basic rates defined by user over time
     if 'rates_import' in self.args:
         self.rate_import = self.basic_rates(self.args['rates_import'])
     if 'rates_export' in self.args:
         self.rate_export = self.basic_rates(self.args['rates_export'])
     
     # Octopus import rates
     if 'metric_octopus_import' in self.args:
         data_import = self.get_state(entity_id = self.args['metric_octopus_import'], attribute='rates')
         
     # Octopus intelligent slots
     if 'octopus_intelligent_slot' in self.args:
         self.octopus_slots = self.get_state(entity_id = self.args['octopus_intelligent_slot'], attribute='completedDispatches')
         self.octopus_slots += self.get_state(entity_id = self.args['octopus_intelligent_slot'], attribute='plannedDispatches')

     # Replicate and scan rates  
     if self.rate_import:
         self.rate_import = self.rate_replicate(self.minute_data(data_import, self.forecast_days, self.midnight_utc, 'rate', 'from', False, False, False, to_key='to'))
         self.rate_import = self.rate_scan(self.rate_import, self.octopus_slots)
         self.publish_rates(self.rate_import, False)
     else:
         self.log("No import rate data provided - using default metric")
     
     # Octopus export rates
     if 'metric_octopus_export' in self.args:
         data_export = self.get_state(entity_id = self.args['metric_octopus_export'], attribute='rates')
         self.rate_export = self.minute_data(data_export, self.forecast_days, self.midnight_utc, 'rate', 'from', False, False, False, to_key='to')
         
     # Replicate rates for export
     if self.rate_export:
         self.rate_export = self.rate_replicate(self.rate_export)
         self.publish_rates(self.rate_export, True)
     else:
         self.log("No export rate data provided - using default metric")
         
     if 'import_today' in self.args and self.rate_import:
         self.import_today = self.minute_data(self.get_history(entity_id = self.args['import_today'], days = 2)[0], 2, now_utc, 'state', 'last_updated', True, True, False)
         self.import_today = self.clean_incrementing_reverse(self.import_today)
         self.today_cost(self.import_today)

     self.reserve = self.soc_max * reserve_percent / 100.0
     self.battery_loss = 1.0 - self.args.get('battery_loss', 0.05)
     self.best_soc_margin = self.args.get('best_soc_margin', 0)
     self.best_soc_min = self.args.get('best_soc_min', 0.5)
     self.best_soc_keep = self.args.get('best_soc_keep', 0.5)
     self.set_soc_minutes = self.args.get('set_soc_minutes', 30)
     self.set_window_minutes = self.args.get('set_window_minutes', 30)
     
     self.charge_enable = self.get_state(self.args['charge_enable'], default = False)
     if self.charge_enable:
        
        charge_start_time = datetime.strptime(self.get_state(self.args['charge_start_time']), "%H:%M:%S")
        charge_end_time = datetime.strptime(self.get_state(self.args['charge_end_time']), "%H:%M:%S")
        charge_start_time_minutes = charge_start_time.hour * 60 + charge_start_time.minute

        # Re-programme charge window based on low rates?
        if self.args.get('set_charge_window', False) and self.low_rates:
           window = self.low_rates[0]
           if window['start'] < 24*60 and window['start'] > self.minutes_now:
               charge_start_time = self.midnight_utc + timedelta(minutes=window['start'])
               charge_end_time = self.midnight_utc + timedelta(minutes=window['end'])
               self.log("Charge window will be: %s - %s" % (charge_start_time, charge_end_time))
                   
               # We must re-program if we are about to run to the new charge window or the old one is about to start
               if ((window['start'] - self.minutes_now) < self.set_window_minutes) or ((charge_start_time_minutes - self.minutes_now) < self.set_window_minutes):
                   self.adjust_charge_window(charge_start_time, charge_end_time)
        
        # Compute charge window minutes start/end just for the next charge window
        self.charge_start_time_minutes = charge_start_time.hour * 60 + charge_start_time.minute
        self.charge_end_time_minutes = charge_end_time.hour * 60 + charge_end_time.minute
        if self.charge_end_time_minutes < self.charge_start_time_minutes:
            self.charge_end_time_minutes += 60 * 24
            
        self.charge_limit = float(self.get_state(self.args['charge_limit'])) * self.soc_max / 100.0
        self.charge_rate = float(self.get_state(self.args['charge_rate'], attribute='max')) / 1000.0 / 60.0
        self.log("Charge settings are: %s-%s limit %s power %s (per minute)" % (str(self.charge_start_time_minutes), str(self.charge_end_time_minutes), str(self.charge_limit), str(self.charge_rate)))
        
        # Save list of charge windows within the simulation time window
        if self.args.get('set_charge_window', False) and self.low_rates:
            # If we are using calculated windows directly then save them
            self.charge_window = self.low_rates
        else:
            # Construct charge window from the GivTCP settings
            self.charge_window = []
            minute = self.charge_start_time_minutes
            minute_end = self.charge_end_time_minutes
            while (minute < self.forecast_minutes):
                window = {}
                window['start'] = minute
                window['end']   = minute_end
                self.charge_window.append(window)
                minute += 24 * 60
                minute_end += 24 * 60
        self.log('Charge windows currently %s' % self.charge_window)
        
     # battery max discharge rate
     self.discharge_rate = float(self.get_state(self.args['discharge_rate'], attribute='max')) / 1000.0 / 60.0
    
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
     self.car_charging_energy = {}
     if 'car_charging_energy' in self.args:
        self.car_charging_energy = self.minute_data(self.get_history(entity_id = self.args['car_charging_energy'], days = self.days_previous + 1)[0], self.days_previous + 1, now_utc, 'state', 'last_updated', True, True, False)
        self.car_charging_energy = self.clean_incrementing_reverse(self.car_charging_energy)
        self.log("Car charging hold %s with energy data" % (self.car_charging_hold))
     else:
        self.log("Car charging hold %s threshold %s" % (self.car_charging_hold, self.car_charging_threshold*60.0))

     # Try different battery SOCs to get the best result
     if self.args.get('calculate_best', False):
        try_soc = self.soc_max
        best_soc = try_soc
        best_metric = 999999
        while try_soc > self.reserve:
            was_debug = self.debug_enable
            self.debug_enable = False
            metric, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min = self.run_prediction(now, try_soc, load_minutes, pv_forecast_minute, False, False)
            self.debug_enable = was_debug
            if self.debug_enable:
                self.log("Trying soc %s gives import battery %s house %s export %s metric %s" % 
                        (try_soc, import_kwh_battery, import_kwh_house, export_kwh, metric))
            
            # Only select the lower SOC if it makes a notable improvement has defined by min_improvement
            # and it doesn't fall below the soc_keep threshold
            if ((metric + self.metric_min_improvement) < best_metric) and (soc_min >= self.best_soc_keep):
                best_metric = metric
                best_soc = try_soc
                if self.debug_enable:
                    self.log("Selecting metric %s soc %s - soc_min %s and keep %s" % (metric, try_soc, soc_min, self.best_soc_keep))
            else:
                if self.debug_enable:
                    self.log("Not Selecting metric %s soc %s - soc_min %s and keep %s" % (metric, try_soc, soc_min, self.best_soc_keep))
            try_soc -= 0.5
         
        # Simulate best - add margin first and clamp to min and then clamp to max
        # Also save the final selected metric
        self.log("Best charge limit (unadjusted) soc calculated at %s (margin added %s and min %s) with metric %s" % (self.dp2(best_soc), self.best_soc_margin, self.best_soc_min, self.dp2(best_metric)))
        best_soc = best_soc + self.best_soc_margin
        best_soc = max(self.best_soc_min, best_soc)
        best_soc = min(best_soc, self.soc_max)
        self.log("Best charge limit (adjusted) soc calculated at %s (margin added %s and min %s) with metric %s" % (self.dp2(best_soc), self.best_soc_margin, self.best_soc_min, self.dp2(best_metric)))
        best_metric, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min = self.run_prediction(now, best_soc, load_minutes, pv_forecast_minute, False, True)
        self.log("Best charging limit soc %s gives import battery %s house %s export %s metric %s" % 
                (self.dp2(best_soc), self.dp2(import_kwh_battery), self.dp2(import_kwh_house), self.dp2(export_kwh), self.dp2(metric)))
        
        # Set the SOC, only do it within the window before the charge starts or during the charge if we change our mind
        if self.args.get('set_soc_enable', False) and self.charge_enable:
            if (self.minutes_now < self.charge_end_time_minutes) and (self.charge_start_time_minutes - self.minutes_now) <= self.set_soc_minutes:
                self.adjust_battery_target(charge_limit_percent)
            else:
                self.log("Not setting charging SOC as we are not within the window (now %s target set_soc_minutes %s charge start time %s" % (self.minutes_now,self.set_soc_minutes, self.charge_start_time_minutes))
     
     # Simulate current settings
     metric, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min = self.run_prediction(now, self.charge_limit, load_minutes, pv_forecast_minute, True, False)
     self.log("Completed run")
     
  def initialize(self):
     self.log("Startup")
     
     # Run every N minutes aligned to the minute
     now = datetime.now()
     midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)     
     self.run_every(self.run_time_loop, midnight, self.args.get('run_every', 5) * 60)
     
     # Do first update now
     self.update_pred()
     
  def run_time_loop(self, cb_args):
     self.update_pred()
  