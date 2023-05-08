"""
Battery Prediction app
see Readme for information
"""
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from datetime import datetime, timedelta
import math
import pytz
import appdaemon.plugins.hass.hassapi as hass
import requests

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
TIME_FORMAT_SECONDS = "%Y-%m-%dT%H:%M:%S.%f%z"
TIME_FORMAT_OCTOPUS = "%Y-%m-%d %H:%M:%S%z"
TRY_AGILE = False # For debugging only - do not use right now, pretends you are on Agile when are you not
MAX_CHARGE_LIMITS = 8

class PredBat(hass.Hass):
    """ 
    The battery prediction class itself 
    """

    def download_octopus_rates(self, url):

        r = requests.get(url)
        data = r.json()        
        mdata = data['results']
        r = requests.get(url + "?page=2")
        data = r.json()  
        mdata += data['results']      
        pdata = self.minute_data(mdata, 2, self.midnight_utc, 'value_inc_vat', 'valid_from', False, False, False, to_key='valid_to')
        return pdata

    def mintes_to_time(self, updated, now):
        """
        Compute the number of minutes between a time (now) and the updated time
        """
        timeday = updated - now
        minutes = int(timeday.seconds / 60) + int(timeday.days * 60*24)
        return minutes

    def minute_data(self, history, days, now, state_key, last_updated_key, format_seconds,
                    backwards, hourly, to_key=None, smoothing=False):
        """
        Turns data from HA into a hash of data indexed by minute with the data being the value
        Can be backwards in time for history (N minutes ago) or forward in time (N minutes in the future)
        """
        mdata = {}
        newest_state = 0
        last_state = 0
        newest_age = 99999
        prev_last_updated_time = None

        if format_seconds:
            format_string = TIME_FORMAT_SECONDS # 2023-04-25T19:33:47.861967+00:00
        else:
            format_string = TIME_FORMAT # 2023-04-25T19:33:47+00:00

        for item in history:
            if state_key not in item:
                continue
            if item[state_key] == 'unavailable' or item[state_key] == 'unknown':
                continue
            state = float(item[state_key])
            if hourly:
                state /= 60
            last_updated = item[last_updated_key]
            last_updated_time = datetime.strptime(last_updated, format_string)

            if not prev_last_updated_time:
                prev_last_updated_time = last_updated_time
                last_state = state

            # Work out end of time period
            # If we don't get it assume it's to the previous update, this is for historical data only (backwards)
            if to_key:
                to_time = datetime.strptime(item[to_key], format_string)
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
        return math.ceil(value*100)/100

    def dp3(self, value):
        """
        Round to 3 decimal places
        """
        return math.ceil(value*1000)/1000

    def in_charge_window(self, charge_window, minute):
        """
        Work out if this minute is within the a charge window
        """
        window_n = 0
        for window in charge_window:
            if minute >= window['start'] and minute < window['end']:
                return window_n
            window_n += 1
        return -1

    def clean_incrementing_reverse(self, data):
        """
        Cleanup an incrementing sensor data that runs backwards in time to remove the
        resets (where it goes back to 0) and make it always increment
        """
        new_data = {}
        length = len(data)

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

    def get_from_incrementing(self, data, index):
        """
        Get a single value from an incrementing series e.g. kwh today -> kwh this minute
        """
        return data[index] - data[index + 1]

    def run_prediction(self, charge_limit, charge_window, load_minutes, pv_forecast_minute, save, save_best):
        """
        Run a prediction scenario given a charge limit, options to save the results or not to HA entity
        """
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
        metric = self.cost_today_sofar
        metric_time = {}
        load_kwh_time = {}

        # For the SOC calculation we need to stop 24 hours after the first charging window starts
        # to avoid wrapping into the next day
        end_record = self.forecast_minutes
        if len(self.charge_window):
            end_record = min(end_record, self.charge_window[0]['start'] + 24*60 - self.minutes_now)
        record = True

        # Simulate each forward minute
        while minute < self.forecast_minutes:
            minute_yesterday = 24 * 60 - minute + six_days
            load_yesterday = self.get_from_incrementing(load_minutes, minute_yesterday)

            minute_absolute = minute + self.minutes_now
            minute_timestamp = self.midnight_utc + timedelta(seconds=60*minute_absolute)
            charge_window_n = self.in_charge_window(charge_window, minute_absolute)

            # Outside the recording window?
            if minute >= end_record and record:
                record = False

            pv_now = pv_forecast_minute.get(minute_absolute, 0.0)

            # Car charging hold
            if self.car_charging_hold and self.car_charging_energy:
                # Hold based on data
                car_energy = self.get_from_incrementing(self.car_charging_energy, minute_yesterday)
                if self.debug_enable and car_energy > 0.0 and (minute % 60) == 0 and (minute < 60*48):
                    self.log("Hour {} car charging hold with data {} load now {} metric {}".format(minute/60, car_energy, load_yesterday, metric))
                load_yesterday = max(0, load_yesterday - car_energy)
            elif self.car_charging_hold and (load_yesterday >= self.car_charging_threshold):
                # Car charging hold - ignore car charging in computation based on threshold
                load_yesterday = 0
                if self.debug_enable and minute % 60 == 0:
                    self.log("Hour {} car charging hold".format(minute/60))

            # Count load
            if record:
                load_kwh += load_yesterday

            # Are we within the charging time window?
            if self.charge_enable and (charge_window_n >= 0) and soc < charge_limit[charge_window_n]:
                old_soc = soc
                soc = min(soc + self.charge_rate, charge_limit[charge_window_n])

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
                    self.log("Hour {} battery charging target soc {}".format(minute/60, charge_limit[charge_window_n]))
            else:
                diff = load_yesterday - pv_now

                # Apply battery loss to charging from PV
                if diff < 0:
                    diff *= self.battery_loss

                # Max charge rate, export over the cap
                if diff < -self.charge_rate:
                    soc -= self.charge_rate
                    if record:
                        energy = -(diff + self.charge_rate)
                        export_kwh += energy
                        if minute_absolute in self.rate_export:
                            metric -= self.rate_export[minute_absolute] * energy
                        else:
                            metric -= self.metric_export * energy

                # Max discharge rate, draw from grid over the cap
                if diff > self.discharge_rate:
                    soc -= self.discharge_rate
                    if record:
                        energy = diff - self.discharge_rate
                        import_kwh += energy
                        if self.charge_enable and (charge_window_n >= 0):
                            # If the battery is on charge anyhow then imports are kind of the same as battery charging (price wise)
                            import_kwh_battery += energy
                        else:
                            # self.log("importing to minute %s amount %s kw total %s kwh total draw %s" % (minute, energy, import_kwh_house, diff))
                            import_kwh_house += energy

                        if minute_absolute in self.rate_import:
                            metric += self.rate_import[minute_absolute] * energy
                        else:
                            if self.charge_enable and (charge_window_n >= 0):
                                metric += self.metric_battery * energy
                            else:
                                metric += self.metric_house * energy
                else:
                    soc -= diff

            # Flat battery, draw from grid over the cap
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

            # Full battery, export over the cap
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
                self.log("Hour {} load_yesterday {} pv_now {} soc {}".format(minute/60, load_yesterday, pv_now, soc))

            predict_soc[minute] = self.dp3(soc)

            # Only store every 10 minutes for data-set size
            if (minute % 10) == 0:
                stamp = minute_timestamp.strftime(TIME_FORMAT)
                predict_soc_time[stamp] = self.dp3(soc)
                metric_time[stamp] = self.dp2(metric)
                load_kwh_time[stamp] = self.dp3(load_kwh)

            # Store the number of minutes until the battery runs out
            if record and soc <= self.reserve:
                minute_left = max(minute, minute_left)

            # Record final soc
            if record:
                final_soc = soc

            # Have we pasted the charging time
            if self.charge_enable and (charge_window_n >= 0):
                charge_has_started = True
            if self.charge_enable and charge_has_started and (charge_window_n < 0):
                charge_has_run = True

            # Record soc min
            if record and (charge_has_run or not self.charge_enable):
                soc_min = min(soc_min, soc)

            minute += 1

        hours_left = minute_left / 60.0
        charge_limit_percent = [min(int((float(charge_limit[i]) / self.soc_max * 100.0) + 0.5), 100) for i in range(0, len(charge_limit))]

        if self.debug_enable or save or save_best:
            self.log("predict charge limit {}% ({} kwh) final soc {} kwh metric {} p min_soc {} kwh".format(charge_limit_percent, self.dp2(charge_limit[0]), self.dp2(final_soc), self.dp2(metric), self.dp2(soc_min)))

        # Save data to HA state
        if save:
            self.set_state("predbat.battery_hours_left", state=self.dp2(hours_left), attributes = {'friendly_name' : 'Predicted Battery Hours left', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'step' : 0.5})
            self.set_state("predbat.soc_kw", state=self.dp3(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Predicted SOC kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
            self.set_state("predbat.soc_min_kwh", state=self.dp3(soc_min), attributes = {'friendly_name' : 'Predicted minimum SOC best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
            self.publish_charge_limit(charge_limit, charge_window, charge_limit_percent, best=False)
            self.set_state("predbat.export_energy", state=self.dp3(export_kwh), attributes = {'friendly_name' : 'Predicted exports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
            self.set_state("predbat.load_energy", state=self.dp3(load_kwh), attributes = {'results' : load_kwh_time, 'friendly_name' : 'Predicted load', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
            self.set_state("predbat.import_energy", state=self.dp3(import_kwh), attributes = {'friendly_name' : 'Predicted imports', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
            self.set_state("predbat.import_energy_battery", state=self.dp3(import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
            self.set_state("predbat.import_energy_house", state=self.dp3(import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
            self.log("Battery has " + str(hours_left) + " hours left - now at " + str(self.soc_kw))
            self.set_state("predbat.metric", state=self.dp2(metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
            self.set_state("predbat.duration", state=self.dp2(end_record/60), attributes = {'friendly_name' : 'Prediction duration', 'state_class': 'measurement', 'unit_of_measurement': 'hours'})

        if save_best:
            self.set_state("predbat.best_battery_hours_left", state=self.dp2(hours_left), attributes = {'friendly_name' : 'Predicted Battery Hours left best', 'state_class': 'measurement', 'unit_of_measurement': 'hours', 'step' : 0.5})
            self.set_state("predbat.soc_kw_best", state=self.dp3(final_soc), attributes = {'results' : predict_soc_time, 'friendly_name' : 'Battery SOC kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
            self.set_state("predbat.best_soc_min_kwh", state=self.dp3(soc_min), attributes = {'friendly_name' : 'Predicted minimum SOC best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh', 'step' : 0.5})
            self.publish_charge_limit(charge_limit, charge_window, charge_limit_percent, best=True)
            self.set_state("predbat.best_export_energy", state=self.dp3(export_kwh), attributes = {'friendly_name' : 'Predicted exports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
            self.set_state("predbat.best_load_energy", state=self.dp3(load_kwh), attributes = {'friendly_name' : 'Predicted load best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
            self.set_state("predbat.best_import_energy", state=self.dp3(import_kwh), attributes = {'friendly_name' : 'Predicted imports best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
            self.set_state("predbat.best_import_energy_battery", state=self.dp3(import_kwh_battery), attributes = {'friendly_name' : 'Predicted import to battery best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
            self.set_state("predbat.best_import_energy_house", state=self.dp3(import_kwh_house), attributes = {'friendly_name' : 'Predicted import to house best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
            self.set_state("predbat.best_metric", state=self.dp2(metric), attributes = {'results' : metric_time, 'friendly_name' : 'Predicted best metric (cost)', 'state_class': 'measurement', 'unit_of_measurement': 'p'})

        return metric, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min

    def adjust_battery_target(self, soc):
        """
        Adjust the battery charging target SOC % in GivTCP
        """
        # Check current setting and adjust
        current_soc = float(self.get_state(entity_id = self.args['soc_percent'], default=100))
        if current_soc != soc:
            self.log("Current SOC is {} and new target is {}".format(current_soc, soc))
            entity_soc = self.get_entity(self.args['soc_percent'])
            if entity_soc:
                entity_soc.call_service("set_value", value=soc)
                if self.args.get('set_soc_notify', False):
                    self.call_service("notify/notify", message='Predbat: Target SOC has been changed to {}'.format(soc))
            else:
                self.log("WARN: Unable to get entity to set SOC target")
        else:
            self.log("Current SOC is {} already at target".format(current_soc))

    def adjust_charge_window(self, charge_start_time, charge_end_time):
        """
        Adjust the charging window times (start and end) in GivTCP
        """
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
            if self.args.get('set_window_notify', False):
                self.call_service("notify/notify", message="Predbat: Charge window change to: {} - {}".format(new_start, new_end))
            self.log("Updated start and end charge window to {} - {} (old {} - {})".format(new_start, new_end, old_start, old_end))

    def rate_replicate(self, rates):
        """
        We don't get enough hours of data for Octopus, so lets assume it repeats until told others
        """
        minute = 0
        # Add 12 extra hours to make sure charging period will end
        while minute < (self.forecast_minutes + 12*60):
            if minute not in rates:
                minute_mod = minute % (24*60)
                if minute_mod in rates:
                    rates[minute] = rates[minute_mod]
                else:
                    # Missing rate within 24 hours - fill with dummy high rate
                    rates[minute] = self.metric_house
            minute += 1
        return rates

    def find_charge_window(self, rates, minute):
        """
        Find the charging windows based on the low rate threshold (percent below average)
        """
        rate_low_start = -1
        rate_low_end = -1
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
        """
        Work out the energy rates based on user supplied time periods
        """
        rates = {}

        # Default to house value
        for minute in range(0, self.forecast_minutes):
            rates[minute] = self.metric_house

        self.log("Adding rate info {}".format(info))
        midnight = datetime.strptime('00:00:00', "%H:%M:%S")
        for this_rate in info:
            start = datetime.strptime(this_rate.get('start', "00:00:00"), "%H:%M:%S")
            end = datetime.strptime(this_rate.get('end', "00:00:00"), "%H:%M:%S")
            rate = this_rate.get('rate', self.metric_house)
            start_minutes = max(self.mintes_to_time(start, midnight), 0)
            end_minutes   = min(self.mintes_to_time(end, midnight), self.forecast_minutes)

            if end_minutes <= start_minutes:
                end_minutes += 24*60

            self.log("Found rate {} {} to {} minutes".format(rate, start_minutes, end_minutes))
            for minute in range(start_minutes, end_minutes):
                rates[minute] = rate

        return rates

    def rate_scan(self, rates, octopus_slots):
        """
        Scan the rates and work out min/max and charging windows
        """
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

        self.log("Rates min {} max {} average {}".format(rate_min, rate_max, rate_average))
        self.rate_min = rate_min
        self.rate_max = rate_max
        self.rate_min_minute = rate_min_minute
        self.rate_max_minute = rate_max_minute
        self.rate_average = rate_average

        # Add in any planned octopus slots
        if octopus_slots:
            for slot in octopus_slots:
                start = datetime.strptime(slot['startDtUtc'], TIME_FORMAT_OCTOPUS)
                end = datetime.strptime(slot['endDtUtc'], TIME_FORMAT_OCTOPUS)
                start_minutes = max(self.mintes_to_time(start, self.midnight_utc), 0)
                end_minutes   = min(self.mintes_to_time(end, self.midnight_utc), self.forecast_minutes)

                self.log("Octopus Intelligent slot at {}-{} assumed price {}".format(start.strftime("%H:%M"), end.strftime("%H:%M"), self.rate_min))
                for minute in range(start_minutes, end_minutes):
                    rates[minute] = self.rate_min
                    if self.debug_enable and (minute % 30) == 0:
                        self.log("Set min octopus rate for time {}".format(minute))

        # Find charging window
        minute = self.minutes_now
        while len(self.low_rates) < MAX_CHARGE_LIMITS:
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

        if self.low_rates:
            window_n = 0
            for window in self.low_rates:
                rate_low_start = window['start']
                rate_low_end = window['end']
                rate_low_average = window['average']

                self.log("Low rate period {}-{} @{} !".format(rate_low_start, rate_low_end, rate_low_average))

                rate_low_start_date = self.midnight_utc + timedelta(minutes=rate_low_start)
                rate_low_end_date = self.midnight_utc + timedelta(minutes=rate_low_end)

                time_format_time = '%H:%M:%S'

                if window_n == 0:
                    self.set_state("predbat.low_rate_start", state=rate_low_start_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next low rate start', 'state_class': 'timestamp'})
                    self.set_state("predbat.low_rate_end", state=rate_low_end_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next low rate end', 'state_class': 'timestamp'})
                    self.set_state("predbat.low_rate_cost", state=rate_low_average, attributes = {'friendly_name' : 'Next low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
                if window_n == 1:
                    self.set_state("predbat.low_rate_start_2", state=rate_low_start_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next+1 low rate start', 'state_class': 'timestamp'})
                    self.set_state("predbat.low_rate_end_2", state=rate_low_end_date.strftime(time_format_time), attributes = {'friendly_name' : 'Next+1 low rate end', 'state_class': 'timestamp'})
                    self.set_state("predbat.low_rate_cost_2", state=rate_low_average, attributes = {'friendly_name' : 'Next+1 low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
                window_n += 1

        # Clear rates that aren't available
        if not self.low_rates:
            self.log("No low rate period found")
            self.set_state("predbat.low_rate_start", state='undefined', attributes = {'friendly_name' : 'Next low rate start', 'device_class': 'timestamp'})
            self.set_state("predbat.low_rate_end", state='undefined', attributes = {'friendly_name' : 'Next low rate end', 'device_class': 'timestamp'})
            self.set_state("predbat.low_rate_cost", state=rate_average, attributes = {'friendly_name' : 'Next low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p'})
        if len(self.low_rates) < 2:
            self.set_state("predbat.low_rate_start_2", state='undefined', attributes = {'friendly_name' : 'Next+1 low rate start', 'device_class': 'timestamp'})
            self.set_state("predbat.low_rate_end_2", state='undefined', attributes = {'friendly_name' : 'Next+1 low rate end', 'device_class': 'timestamp'})
            self.set_state("predbat.low_rate_cost_2", state=rate_average, attributes = {'friendly_name' : 'Next+1 low rate cost', 'state_class': 'measurement', 'unit_of_measurement': 'p'})

        return rates

    def publish_rates(self, rates, export):
        """
        Publish the rates for charts
        Create rates/time every 30 minutes
        """
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

    def today_cost(self, import_today):
        """
        Work out energy costs today (approx)
        """
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
                day_cost_time[stamp] = self.dp2(day_cost)

        self.set_state("predbat.cost_today", state=self.dp2(day_cost), attributes = {'results' : day_cost_time, 'friendly_name' : 'Cost so far today', 'state_class' : 'measurement', 'unit_of_measurement': 'p'})
        self.log("Todays energy {} kwh cost {} p".format(self.dp2(day_energy), self.dp2(day_cost)))
        return day_cost

    def publish_charge_limit(self, charge_limit, charge_window, charge_limit_percent, best):

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
        
        if best:
            self.set_state("predbat.best_charge_limit_kw", state=self.dp2(charge_limit[0]), attributes = {'results' : charge_limit_time_kw, 'friendly_name' : 'Predicted charge limit kwh best', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
            self.set_state("predbat.best_charge_limit", state=charge_limit_percent[0], attributes = {'results' : charge_limit_time, 'friendly_name' : 'Predicted charge limit best', 'state_class': 'measurement', 'unit_of_measurement': '%'})
        else:
            self.set_state("predbat.charge_limit_kw", state=self.dp2(charge_limit[0]), attributes = {'results' : charge_limit_time_kw, 'friendly_name' : 'Predicted charge limit kwh', 'state_class': 'measurement', 'unit_of_measurement': 'kwh'})
            self.set_state("predbat.charge_limit", state=charge_limit_percent[0], attributes = {'results' : charge_limit_time, 'friendly_name' : 'Predicted charge limit', 'state_class': 'measurement', 'unit_of_measurement': '%'})

    def reset(self):
        """
        Init stub
        """
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
        self.metric_house = 0
        self.metric_battery = 0
        self.metric_export = 0
        self.metric_min_improvement = 0
        self.rate_import = {}
        self.rate_export = {}
        self.rate_slots = []
        self.low_rates = []
        self.cost_today_sofar = 0
        self.octopus_slots = []
        self.reserve = 0
        self.battery_loss = 0
        self.best_soc_min = 0
        self.best_soc_margin = 0
        self.best_soc_keep = 0
        self.rate_min = 0
        self.rate_min_minute = 0
        self.rate_max = 0
        self.rate_max_minute = 0
        self.rate_average = 0
        self.set_soc_minutes = 0
        self.set_window_minutes = 0
        self.debug_enable = False
        self.import_today = {}
        self.charge_enable = False
        self.charge_start_time_minutes = 0
        self.charge_end_time_minutes = 0
        self.charge_limit = []
        self.charge_rate = 4000
        self.discharge_rate = 4000
        self.charge_window = []
        self.car_charging_hold = False
        self.car_charging_threshold = 99
        self.car_charging_energy = {}

    def optimise_charge_limit(self, window_n, charge_window, try_charge_limit, load_minutes, pv_forecast_minute):
        """
        Optimise a single charging window for best SOC
        """
        loop_soc = self.soc_max
        best_soc = self.soc_max
        best_metric = 9999999
        
        while loop_soc > self.reserve:
            was_debug = self.debug_enable
            self.debug_enable = False

            # Apply user clamping to the value we try
            try_soc = min(loop_soc + self.best_soc_margin, self.soc_max)
            try_soc = max(self.best_soc_min, try_soc)
            try_soc = self.dp2(min(try_soc, self.soc_max))

            # Store try value into the dinwo
            try_charge_limit[window_n] = try_soc

            # Simulate
            metric, charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min = self.run_prediction(try_charge_limit, charge_window, load_minutes, pv_forecast_minute, False, False)
            self.debug_enable = was_debug
            if self.debug_enable:
                self.log("Trying soc {} for window {} gives import battery {} house {} export {} metric {}".format
                        (try_soc, window_n, import_kwh_battery, import_kwh_house, export_kwh, metric))

            # Only select the lower SOC if it makes a notable improvement has defined by min_improvement (divided in M windows)
            # and it doesn't fall below the soc_keep threshold
            if ((metric + (self.metric_min_improvement / len(charge_window)) < best_metric)) and (soc_min >= self.best_soc_keep):
                best_metric = metric
                best_soc = try_soc
                if self.debug_enable:
                    self.log("Selecting metric {} soc {} - soc_min {} and keep {}".format(metric, try_soc, soc_min, self.best_soc_keep))
            else:
                if self.debug_enable:
                    self.log("Not Selecting metric {} soc {} - soc_min {} and keep {}".format(metric, try_soc, soc_min, self.best_soc_keep))
            loop_soc -= 0.5

        # Add margin first and clamp to min and then clamp to max
        # Also save the final selected metric

        return best_soc, best_metric

    def update_pred(self):
        """
        Update the prediction state, everything is called from here right now
        """
        local_tz = pytz.timezone(self.args.get('timezone', "Europe/London"))
        now_utc = datetime.now(local_tz)
        now = datetime.now()
        self.log("PredBat - update at: " + str(now_utc))

        self.debug_enable = self.args.get('debug_enable', False)
        self.log("Debug enable is {}".format(self.debug_enable))

        self.midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        self.midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        self.difference_minutes = self.minutes_since_yesterday(now)
        self.minutes_now = int((now - self.midnight).seconds / 60)
        self.minutes_to_midnight = 24*60 - self.minutes_now

        self.days_previous = self.args.get('days_previous', 7)
        forecast_hours = self.args.get('forecast_hours', 24)
        self.forecast_days = int((forecast_hours + 23)/24)
        self.forecast_minutes = forecast_hours * 60

        load_minutes = self.minute_data(self.get_history(entity_id = self.args['load_today'], days = self.days_previous + 1)[0], self.days_previous + 1, now_utc, 'state', 'last_updated', True, True, False, smoothing=True)
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
        self.octopus_slots = []
        self.cost_today_sofar = 0

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
            completed = self.get_state(entity_id = self.args['octopus_intelligent_slot'], attribute='completedDispatches')
            if completed:
                self.octopus_slots += planned
            planned = self.get_state(entity_id = self.args['octopus_intelligent_slot'], attribute='plannedDispatches')
            if planned:
                self.octopus_slots += planned

        # Replicate and scan rates
        if self.rate_import:
            if TRY_AGILE:
                self.rate_import = self.rate_replicate(self.download_octopus_rates("https://api.octopus.energy/v1/products/AGILE-FLEX-22-11-25/electricity-tariffs/E-1R-AGILE-FLEX-22-11-25-H/standard-unit-rates/"))
                self.octopus_slots = []
            else:
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

        # Load import today data and work out cost so far
        if 'import_today' in self.args and self.rate_import:
            self.import_today = self.minute_data(self.get_history(entity_id = self.args['import_today'], days = 2)[0], 2, now_utc, 'state', 'last_updated', True, True, False, smoothing=True)
            self.import_today = self.clean_incrementing_reverse(self.import_today)
            self.cost_today_sofar = self.today_cost(self.import_today)

        # Battery charging options
        self.reserve = self.soc_max * reserve_percent / 100.0
        self.battery_loss = 1.0 - self.args.get('battery_loss', 0.05)
        self.best_soc_margin = self.args.get('best_soc_margin', 0)
        self.best_soc_min = self.args.get('best_soc_min', 0.5)
        self.best_soc_keep = self.args.get('best_soc_keep', 0.5)
        self.set_soc_minutes = self.args.get('set_soc_minutes', 30)
        self.set_window_minutes = self.args.get('set_window_minutes', 30)

        self.charge_enable = self.get_state(self.args['charge_enable'], default = False)

        # If the battery is being charged then find the charge window
        if self.charge_enable:
            # Find current charge window
            charge_start_time = datetime.strptime(self.get_state(self.args['charge_start_time']), "%H:%M:%S")
            charge_end_time = datetime.strptime(self.get_state(self.args['charge_end_time']), "%H:%M:%S")
            charge_start_time_minutes = charge_start_time.hour * 60 + charge_start_time.minute

            # Compute charge window minutes start/end just for the next charge window
            self.charge_start_time_minutes = charge_start_time.hour * 60 + charge_start_time.minute
            self.charge_end_time_minutes = charge_end_time.hour * 60 + charge_end_time.minute
            if self.charge_end_time_minutes < self.charge_start_time_minutes:
                self.charge_end_time_minutes += 60 * 24

            # Construct charge window from the GivTCP settings
            self.charge_window = []
            minute = self.charge_start_time_minutes
            minute_end = self.charge_end_time_minutes

            while minute < self.forecast_minutes:
                window = {}
                window['start'] = minute
                window['end']   = minute_end
                self.charge_window.append(window)
                minute += 24 * 60
                minute_end += 24 * 60
            self.log('Charge windows currently {}'.format(self.charge_window))
            
            # Calculate best charge windows
            if self.args.get('set_charge_window', False) and self.low_rates:
                # If we are using calculated windows directly then save them
                self.charge_window_best = self.low_rates[:]
            else:
                # Default best charge window as this one
                self.charge_window_best = self.charge_window[:]

            # Get charge limit and fill for the number of windows
            current_charge_limit = float(self.get_state(self.args['charge_limit']))
            self.charge_limit = [current_charge_limit * self.soc_max / 100.0 for i in range(0, len(self.charge_window))]
            self.charge_limit_percent = [current_charge_limit for i in range(0, len(self.charge_window))]

            self.charge_limit_best = [current_charge_limit * self.soc_max / 100.0 for i in range(0, len(self.charge_window_best))]
            self.charge_limit_percent_best = [current_charge_limit for i in range(0, len(self.charge_window_best))]

            self.charge_rate = float(self.get_state(self.args['charge_rate'], attribute='max')) / 1000.0 / 60.0
            self.log("Charge settings are: {}-{} limit {} power {} (per minute)".format(str(self.charge_start_time_minutes), str(self.charge_end_time_minutes), str(self.charge_limit[0]), str(self.charge_rate)))

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
            self.car_charging_energy = self.minute_data(self.get_history(entity_id = self.args['car_charging_energy'], days = self.days_previous + 1)[0], self.days_previous + 1, now_utc, 'state', 'last_updated', True, True, False, smoothing=True)
            self.car_charging_energy = self.clean_incrementing_reverse(self.car_charging_energy)
            self.log("Car charging hold {} with energy data".format(self.car_charging_hold))
        else:
            self.log("Car charging hold {} threshold {}".format(self.car_charging_hold, self.car_charging_threshold*60.0))

        # Simulate current settings
        metric, self.charge_limit_percent, import_kwh_battery, import_kwh_house, export_kwh, soc_min = self.run_prediction(self.charge_limit, self.charge_window, load_minutes, pv_forecast_minute, True, False)

        # Default best
        self.charge_limit_percent_best = self.charge_limit_percent[:]

        # Try different battery SOCs to get the best result
        if self.args.get('calculate_best', False):
            # Set all to 100%
            self.charge_limit_best = [self.soc_max for n in range(0, len(self.charge_limit_best))]
            for window_n in range(0, len(self.charge_limit_best)):
                self.log("optiming charge window {}".format(window_n))
                best_soc, best_metric = self.optimise_charge_limit(window_n, self.charge_window_best, self.charge_limit_best, load_minutes, pv_forecast_minute)

                #if self.debug_enable:
                self.log("Best charge limit window {} (adjusted) soc calculated at {} (margin added {} and min {}) with metric {}".format(window_n, self.dp2(best_soc), self.best_soc_margin, self.best_soc_min, self.dp2(best_metric)))
                self.charge_limit_best[window_n] = best_soc

        # Final simulation of best
        best_metric, self.charge_limit_percent_best, import_kwh_battery, import_kwh_house, export_kwh, soc_min = self.run_prediction(self.charge_limit_best, self.charge_window_best, load_minutes, pv_forecast_minute, False, True)
        self.log("Best charging limit socs {} gives import battery {} house {} export {} metric {}".format
        (self.charge_limit_best, self.dp2(import_kwh_battery), self.dp2(import_kwh_house), self.dp2(export_kwh), self.dp2(best_metric)))

        if self.charge_enable:
            # Re-programme charge window based on low rates?
            if self.args.get('set_charge_window', False) and self.low_rates:
                window = self.charge_window_best[0]
                self.charge_start_time_minutes = window['start']
                self.charge_end_time_minutes = window['end']

                if window['start'] < 24*60 and window['start'] > self.minutes_now:
                    charge_start_time = self.midnight_utc + timedelta(minutes=window['start'])
                    charge_end_time = self.midnight_utc + timedelta(minutes=window['end'])
                    self.log("Charge window will be: {} - {}".format(charge_start_time, charge_end_time))

                    # We must re-program if we are about to run to the new charge window or the old one is about to start
                    if ((window['start'] - self.minutes_now) < self.set_window_minutes) or ((charge_start_time_minutes - self.minutes_now) < self.set_window_minutes):
                        self.adjust_charge_window(charge_start_time, charge_end_time)
            
            # Set the SOC, only do it within the window before the charge starts or during the charge if we change our mind
            if self.args.get('set_soc_enable', False):
                if (self.minutes_now < self.charge_end_time_minutes) and (self.charge_start_time_minutes - self.minutes_now) <= self.set_soc_minutes:
                    self.adjust_battery_target(self.charge_limit_percent_best[0])
                else:
                    self.log("Not setting charging SOC as we are not within the window (now {} target set_soc_minutes {} charge start time {}".format(self.minutes_now,self.set_soc_minutes, self.charge_start_time_minutes))


        self.log("Completed run")

    async def initialize(self):
        """
        Setup the app, called once each time the app starts
        """
        self.log("Predbat Startup")
        self.reset()

        # Run every N minutes aligned to the minute
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # First run is now
        self.run_in(self.run_time_loop, 0)

        # And then every N minutes
        self.run_every(self.run_time_loop, midnight, self.args.get('run_every', 5) * 60)

    def run_time_loop(self, cb_args):
        """
        Called every N minutes
        """
        self.update_pred()
    