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

  def update_pred(self):
     local_tz = get_localzone()      
     now_utc = datetime.now(local_tz) #timezone.utc)
     now = datetime.now()
     self.log("PredBat - update at: " + str(now_utc))
     
     midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
     midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
     
     load_minutes = self.minute_data(self.get_history(entity_id = self.args['load_today'], days = 8)[0], 8, now_utc, 'state', 'last_updated', True, True, False)
     soc_kw = float(self.get_state(entity_id = self.args['soc_kw'], default=0))
     soc_max = float(self.get_state(entity_id = self.args['soc_max'], default=0))
     reserve_percent = float(self.get_state(entity_id = self.args['reserve'], default=0))
     reserve = soc_max * reserve_percent / 100.0
     
     charge_enable = self.get_state(self.args['charge_enable'], default = False)
     if charge_enable:
        charge_start_time = datetime.strptime(self.get_state(self.args['charge_start_time']), "%H:%M:%S")
        charge_start_time_minutes = charge_start_time.hour * 60 + charge_start_time.minute
        charge_end_time = datetime.strptime(self.get_state(self.args['charge_end_time']), "%H:%M:%S")
        charge_end_time_minutes = charge_end_time.hour * 60 + charge_end_time.minute
        if charge_end_time_minutes < charge_start_time_minutes:
            charge_end_time_minutes += 60*24
            
        charge_limit = float(self.get_state(self.args['charge_limit'])) * soc_max / 100.0
        charge_rate = float(self.get_state(self.args['charge_rate'])) / 1000.0 / 60.0
        self.log("Charge settings are: %s-%s limit %s power %s (per minute)" % (str(charge_start_time_minutes), str(charge_end_time_minutes), str(charge_limit), str(charge_rate)))
        
     # battery max discharge rate
     discharge_rate = float(self.get_state(self.args['discharge_rate'])) / 1000.0 / 60.0
    
     if 'pv_forecast_today' in self.args:
        pv_forecast_data_today    = self.get_state(entity_id = self.args['pv_forecast_today'], attribute='forecast')
        pv_forecast_data_tomorrow = self.get_state(entity_id = self.args['pv_forecast_tomorrow'], attribute='forecast')
        pv_forecast_minute = self.minute_data(pv_forecast_data_today + pv_forecast_data_tomorrow, 48, midnight_utc, 'pv_estimate', 'period_start', False, False, True)
     else:
        pv_forecast = 0.0
        pv_forecast_data = {}
        pv_forecast_minute = {}

     # Car charging hold - when enabled battery is held during car charging in simulation
     car_charging_hold = self.args.get('car_charging_hold', False)
     car_charging_threshold = float(self.args.get('car_charging_threshold', 6.0)) / 60.0
     debug_enable = self.args.get('debug_enable', False)
     self.log("Car charging hold %s threshold %s" % (car_charging_hold, car_charging_threshold*60.0))

     # Compute times
     difference_minutes = self.minutes_since_yesterday(now)
     minutes_now = int((now - midnight).seconds / 60)
     minutes_to_midnight = 24*60 - minutes_now
     six_days = 24*60*6
     
     # Offset by 6 days to get to last week
     load_yesterday = load_minutes[difference_minutes + six_days]
     load_yesterday_now = load_minutes[24*60 + six_days]
     self.log("Minutes since yesterday " + str(difference_minutes) + " load past day " + str(load_yesterday) + " load past day now " + str(load_yesterday_now))
     
     predict_soc = {}
     predict_soc_time = {}
     minute = 0
     minute_left = 24*60
     soc = soc_kw
     export_kwh = 0
     import_kwh = 0
     import_kwh_house = 0
     import_kwh_battery = 0
     
     while minute < 24*60:
         
        minute_yesterday = 24*60 - minute + six_days
        # Average previous load over 10 minutes due to sampling accuracy
        load_yesterday = (load_minutes[minute_yesterday] - load_minutes[minute_yesterday + 10]) / 10.0
        
        # Resets at midnight so avoid wrap
        if load_yesterday < 0:
            load_yesterday = load_minutes[minute_yesterday]
            
        minute_absolute = minute + minutes_now
        minute_timestamp = midnight_utc + timedelta(seconds=60*minute_absolute)
        
        if minute_absolute in pv_forecast_minute:
            pv_now = pv_forecast_minute[minute_absolute]
        else:
            pv_now = 0
            
        if car_charging_hold and (load_yesterday >= car_charging_threshold):
            # Car charging hold - ignore car charging in computation
            load_yesterday = 0
            if debug_enable and minute % 15 == 0:
                self.log("Hour %s car charging hold" % (minute/60))
            
        if charge_enable and soc < charge_limit and minute_absolute >= charge_start_time_minutes and minute_absolute < charge_end_time_minutes:
            old_soc = soc
            soc = min(soc + charge_rate, charge_limit)
            import_kwh += max(0, soc - old_soc - pv_now)
            import_kwh_battery += max(0, soc - old_soc - pv_now)
            if debug_enable and minute % 15 == 0:
                self.log("Hour %s battery charging target soc %s" % (minute/60, charge_limit))
        else:
            diff = load_yesterday - pv_now
            if diff > discharge_rate:
                soc -= discharge_rate
                import_kwh += (diff - discharge_rate)
                import_kwh_house += (diff - discharge_rate)
            else:
                soc -= diff
                
        if soc < reserve:
            import_kwh += reserve - soc 
            import_kwh_house += reserve - soc
            soc = reserve
            
        if soc > soc_max:
            export_kwh += soc - soc_max
            soc = soc_max
        
        if debug_enable and minute % 15 == 0:
            self.log("Hour %s load_yesterday %s pv_now %s soc %s" % (minute/60, load_yesterday, pv_now, soc))
        
        predict_soc[minute] = soc
        predict_soc_time[str(minute_timestamp)] = soc
        
        # Store the worst caste
        if soc <= reserve:
            if minute_left > minute:
                minute_left = minute
        minute += 1
        
     #self.log("load yesterday " + str(load_minutes))
     #self.log("predict soc " + str(predict_soc_time))

     hours_left = minute_left / 60.0

     self.set_state("predbat.battery_hours_left", state=self.dp2(hours_left), attributes = {'unique_id': 'predbat0001', 'friendly_name' : 'Battery Hours left', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'step' : 0.5})
     self.set_state("predbat.soc_kw", state=self.dp2(soc), attributes = {'unique_id': 'predbat0001', 'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
     self.set_state("predbat.export_energy", state=self.dp2(export_kwh), attributes = {'unique_id': 'predbat0002', 'friendly_name' : 'Predicted exports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
     self.set_state("predbat.import_energy", state=self.dp2(import_kwh), attributes = {'unique_id': 'predbat0003', 'friendly_name' : 'Predicted imports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
     self.set_state("predbat.import_energy_battery", state=self.dp2(import_kwh_battery), attributes = {'unique_id': 'predbat0003', 'friendly_name' : 'Predicted import to battery', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
     self.set_state("predbat.import_energy_house", state=self.dp2(import_kwh_house), attributes = {'unique_id': 'predbat0003', 'friendly_name' : 'Predicted import to house', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
     self.log("Battery has " + str(hours_left) + " hours left - now at " + str(soc_kw))
  def initialize(self):
     self.log("Startup")
     # Run every 5 minutes
     self.run_every(self.run_time_loop, "now", 5 * 60)
     
  def run_time_loop(self, cb_args):
     self.update_pred()
  