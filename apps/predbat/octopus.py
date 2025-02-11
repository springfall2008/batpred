# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

import requests
import re
from datetime import datetime, timedelta
from config import TIME_FORMAT, TIME_FORMAT_OCTOPUS
from utils import str2time, minutes_to_time, dp1, dp2


class Octopus:
    def octopus_free_line(self, res, free_sessions):
        """
        Parse a line from the octopus free data
        """
        if res:
            dayname = res.group(1)
            daynumber = res.group(2)
            daysymbol = res.group(3)
            month = res.group(4)
            time_from = res.group(5)
            time_to = res.group(6)
            if "pm" in time_to:
                is_pm = True
            else:
                is_pm = False
            if "pm" in time_from:
                is_fpm = True
            elif "am" in time_from:
                is_fpm = False
            else:
                is_fpm = is_pm
            time_from = time_from.replace("am", "")
            time_from = time_from.replace("pm", "")
            time_to = time_to.replace("am", "")
            time_to = time_to.replace("pm", "")
            try:
                time_from = int(time_from)
                time_to = int(time_to)
            except (ValueError, TypeError):
                return
            if is_fpm:
                time_from += 12
            if is_pm:
                time_to += 12
            # Convert into timestamp object
            now = datetime.now()
            year = now.year
            time_from = str(time_from)
            time_to = str(time_to)
            daynumber = str(daynumber)
            if len(time_from) == 1:
                time_from = "0" + time_from
            if len(time_to) == 1:
                time_to = "0" + time_to
            if len(daynumber) == 1:
                daynumber = "0" + daynumber

            try:
                timestamp_start = datetime.strptime("{} {} {} {} {} Z".format(year, month, daynumber, str(time_from), "00"), "%Y %B %d %H %M %z")
                timestamp_end = datetime.strptime("{} {} {} {} {} Z".format(year, month, daynumber, str(time_to), "00"), "%Y %B %d %H %M %z")
                # Change to local timezone, but these times were in local zone so push the hour back to the correct one
                timestamp_start = timestamp_start.astimezone(self.local_tz)
                timestamp_end = timestamp_end.astimezone(self.local_tz)
                timestamp_start = timestamp_start.replace(hour=int(time_from))
                timestamp_end = timestamp_end.replace(hour=int(time_to))
                free_sessions.append({"start": timestamp_start.strftime(TIME_FORMAT), "end": timestamp_end.strftime(TIME_FORMAT), "rate": 0.0})
            except (ValueError, TypeError) as e:
                pass

    def download_octopus_free_func(self, url):
        """
        Download octopus free session data directly from a URL
        """
        # Check the cache first
        now = datetime.now()
        if url in self.octopus_url_cache:
            stamp = self.octopus_url_cache[url]["stamp"]
            pdata = self.octopus_url_cache[url]["data"]
            age = now - stamp
            if age.seconds < (30 * 60):
                self.log("Return cached octopus data for {} age {} minutes".format(url, dp1(age.seconds / 60)))
                return pdata

        r = requests.get(url)
        if r.status_code not in [200, 201]:
            self.log("Warn: Error downloading Octopus data from URL {}, code {}".format(url, r.status_code))
            self.record_status("Warn: Error downloading Octopus free session data", debug=url, had_errors=True)
            return None

        # Return new data
        self.octopus_url_cache[url] = {}
        self.octopus_url_cache[url]["stamp"] = now
        self.octopus_url_cache[url]["data"] = r.text
        return r.text

    def download_octopus_free(self, url):
        """
        Download octopus free session data directly from a URL and process the data
        """

        free_sessions = []
        pdata = self.download_octopus_free_func(url)
        if not pdata:
            return free_sessions

        for line in pdata.split("\n"):
            if "Past sessions" in line:
                future_line = line.split("<p data-block-key")
                for fline in future_line:
                    res = re.search(r"<i>\s*(\S+)\s+(\d+)(\S+)\s+(\S+)\s+(\S+)-(\S+)\s*</i>", fline)
                    self.octopus_free_line(res, free_sessions)
            if "Free Electricity:" in line:
                # Free Electricity: Sunday 24th November 7-9am
                res = re.search(r"Free Electricity:\s+(\S+)\s+(\d+)(\S+)\s+(\S+)\s+(\S+)-(\S+)", line)
                self.octopus_free_line(res, free_sessions)
        return free_sessions

    def download_octopus_rates(self, url):
        """
        Download octopus rates directly from a URL or return from cache if recent
        Retry 3 times and then throw error
        """

        self.log("Download Octopus rates from {}".format(url))

        # Check the cache first
        now = datetime.now()
        if url in self.octopus_url_cache:
            stamp = self.octopus_url_cache[url]["stamp"]
            pdata = self.octopus_url_cache[url]["data"]
            age = now - stamp
            if age.seconds < (30 * 60):
                self.log("Return cached octopus data for {} age {} minutes".format(url, dp1(age.seconds / 60)))
                return pdata

        # Retry up to 3 minutes
        for retry in range(3):
            pdata = self.download_octopus_rates_func(url)
            if pdata:
                break

        # Download failed?
        if not pdata:
            self.log("Warn: Unable to download Octopus data from URL {} (data empty)".format(url))
            self.record_status("Warn: Unable to download Octopus data from cloud", debug=url, had_errors=True)
            if url in self.octopus_url_cache:
                pdata = self.octopus_url_cache[url]["data"]
                return pdata
            else:
                raise ValueError

        # Cache New Octopus data
        self.octopus_url_cache[url] = {}
        self.octopus_url_cache[url]["stamp"] = now
        self.octopus_url_cache[url]["data"] = pdata
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
            if r.status_code not in [200, 201]:
                self.log("Warn: Error downloading Octopus data from URL {}, code {}".format(url, r.status_code))
                self.record_status("Warn: Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            try:
                data = r.json()
            except requests.exceptions.JSONDecodeError:
                self.log("Warn: Error downloading Octopus data from URL {} (JSONDecodeError)".format(url))
                self.record_status("Warn: Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            if "results" in data:
                mdata += data["results"]
            else:
                self.log("Warn: Error downloading Octopus data from URL {} (No Results)".format(url))
                self.record_status("Warn: Error downloading Octopus data from cloud", debug=url, had_errors=True)
                return {}
            url = data.get("next", None)
            pages += 1

        pdata = self.minute_data(mdata, self.forecast_days + 1, self.midnight_utc, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
        return pdata

    def add_now_to_octopus_slot(self, octopus_slots, now_utc):
        """
        For intelligent charging, add in if the car is charging now as a low rate slot (workaround for Ohme)
        """
        for car_n in range(self.num_cars):
            if self.car_charging_now[car_n]:
                minutes_start_slot = int(self.minutes_now / 30) * 30
                minutes_end_slot = minutes_start_slot + 30
                slot_start_date = self.midnight_utc + timedelta(minutes=minutes_start_slot)
                slot_end_date = self.midnight_utc + timedelta(minutes=minutes_end_slot)
                slot = {}
                slot["start"] = slot_start_date.strftime(TIME_FORMAT)
                slot["end"] = slot_end_date.strftime(TIME_FORMAT)
                octopus_slots.append(slot)
                self.log("Car is charging now - added new IO slot {}".format(slot))
        return octopus_slots

    def load_free_slot(self, octopus_free_slots, export=False, rate_replicate={}):
        """
        Load octopus free session slot
        """
        start_minutes = 0
        end_minutes = 0

        for octopus_free_slot in octopus_free_slots:
            start = octopus_free_slot["start"]
            end = octopus_free_slot["end"]
            rate = octopus_free_slot["rate"]

            if start and end:
                try:
                    start = str2time(start)
                    end = str2time(end)
                except (ValueError, TypeError):
                    start = None
                    end = None
                    self.log("Warn: Unable to decode Octopus free session start/end time")

            if start and end:
                start_minutes = minutes_to_time(start, self.midnight_utc)
                end_minutes = min(minutes_to_time(end, self.midnight_utc), self.forecast_minutes)

            if start_minutes >= 0 and end_minutes != start_minutes and start_minutes < self.forecast_minutes:
                self.log("Setting Octopus free session in range {} - {} export {} rate {}".format(self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), export, rate))
                for minute in range(start_minutes, end_minutes):
                    if export:
                        self.rate_export[minute] = rate
                    else:
                        self.rate_import[minute] = min(rate, self.rate_import[minute])
                        self.load_scaling_dynamic[minute] = self.load_scaling_free
                    rate_replicate[minute] = "saving"

    def load_saving_slot(self, octopus_saving_slots, export=False, rate_replicate={}):
        """
        Load octopus saving session slot
        """
        start_minutes = 0
        end_minutes = 0

        for octopus_saving_slot in octopus_saving_slots:
            start = octopus_saving_slot["start"]
            end = octopus_saving_slot["end"]
            rate = octopus_saving_slot["rate"]
            state = octopus_saving_slot["state"]

            if start and end:
                try:
                    start = str2time(start)
                    end = str2time(end)
                except (ValueError, TypeError):
                    start = None
                    end = None
                    self.log("Warn: Unable to decode Octopus saving session start/end time")
            if state and (not start or not end):
                self.log("Currently in saving session, assume current 30 minute slot")
                start_minutes = int(self.minutes_now / 30) * 30
                end_minutes = start_minutes + 30
            elif start and end:
                start_minutes = minutes_to_time(start, self.midnight_utc)
                end_minutes = min(minutes_to_time(end, self.midnight_utc), self.forecast_minutes)

            if start_minutes < self.forecast_minutes and ((export and (start_minutes in self.rate_export)) or (not export and (start_minutes in self.rate_import))):
                self.log("Setting Octopus saving session in range {} - {} export {} rate {}".format(self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), export, rate))
                for minute in range(start_minutes, end_minutes):
                    if export:
                        self.rate_export[minute] += rate
                    else:
                        self.rate_import[minute] += rate
                        self.load_scaling_dynamic[minute] = self.load_scaling_saving
                    rate_replicate[minute] = "saving"

    def decode_octopus_slot(self, slot, raw=False):
        """
        Decode IOG slot
        """
        if "start" in slot:
            start = datetime.strptime(slot["start"], TIME_FORMAT)
            end = datetime.strptime(slot["end"], TIME_FORMAT)
        else:
            start = datetime.strptime(slot["startDtUtc"], TIME_FORMAT_OCTOPUS)
            end = datetime.strptime(slot["endDtUtc"], TIME_FORMAT_OCTOPUS)

        source = slot.get("source", "")
        location = slot.get("location", "")

        start_minutes = minutes_to_time(start, self.midnight_utc)
        end_minutes = minutes_to_time(end, self.midnight_utc)
        org_minutes = end_minutes - start_minutes

        # Cap slot times into the forecast itself
        if not raw:
            start_minutes = max(start_minutes, 0)
            end_minutes = max(min(end_minutes, self.forecast_minutes + self.minutes_now), start_minutes)

        if start_minutes == end_minutes:
            return 0, 0, 0, source, location

        cap_minutes = end_minutes - start_minutes
        cap_hours = cap_minutes / 60

        # The load expected is stored in chargeKwh for the period in use
        if "charge_in_kwh" in slot:
            kwh = abs(float(slot.get("charge_in_kwh", 0.0)))
        else:
            kwh = abs(float(slot.get("chargeKwh", 0.0)))

        if not kwh:
            kwh = self.car_charging_rate[0] * cap_hours
        else:
            kwh = kwh * cap_minutes / org_minutes

        return start_minutes, end_minutes, kwh, source, location

    def load_octopus_slots(self, octopus_slots, octopus_intelligent_consider_full):
        """
        Turn octopus slots into charging plan
        """
        new_slots = []
        octopus_slot_low_rate = self.get_arg("octopus_slot_low_rate", True)
        car_soc = self.car_charging_soc[0]
        limit = self.car_charging_limit[0]
        slots_decoded = []

        # Decode the slots
        for slot in octopus_slots:
            start_minutes, end_minutes, kwh, source, location = self.decode_octopus_slot(slot)
            slots_decoded.append((start_minutes, end_minutes, kwh, source, location))

        # Sort slots by start time
        slots_sorted = sorted(slots_decoded, key=lambda x: x[0])
        
        # Add in the current charging slot
        for slot in slots_sorted:
            start_minutes, end_minutes, kwh, source, location = slot
            kwh_original = kwh
            end_minutes_original = end_minutes
            if (end_minutes > start_minutes) and (end_minutes > self.minutes_now) and (not location or location == "AT_HOME"):
                kwh_expected = kwh * self.car_charging_loss
                if octopus_intelligent_consider_full:
                    kwh_expected = max(min(kwh_expected, limit - car_soc), 0)
                    kwh = dp2(kwh_expected / self.car_charging_loss)

                # Remove the remaining unused time
                if octopus_intelligent_consider_full and kwh > 0 and (min(car_soc + kwh_expected, limit) >= limit):
                    required_extra_soc = max(limit - car_soc, 0)
                    required_minutes = int(required_extra_soc / (kwh_original * self.car_charging_loss) * (end_minutes - start_minutes) + 0.5)
                    required_minutes = min(required_minutes, end_minutes - start_minutes)
                    end_minutes = start_minutes + required_minutes
                    end_minutes = int((end_minutes + 29) / 30) * 30 # Round up to 30 minutes

                    car_soc = min(car_soc + kwh_expected, limit)
                    new_slot = {}
                    new_slot["start"] = start_minutes
                    new_slot["end"] = end_minutes
                    new_slot["kwh"] = kwh
                    new_slot["average"] = self.rate_import.get(start_minutes, self.rate_min)
                    if octopus_slot_low_rate and source != "bump-charge":
                        new_slot["average"] = self.rate_min  # Assume price in min
                    new_slot["cost"] = new_slot["average"] * kwh
                    new_slot["soc"] = car_soc
                    new_slots.append(new_slot)

                    if end_minutes_original > end_minutes:
                        new_slot = {}
                        new_slot["start"] = end_minutes
                        new_slot["end"] = end_minutes_original
                        new_slot["kwh"] = 0.0
                        new_slot["average"] = self.rate_import.get(start_minutes, self.rate_min)
                        if octopus_slot_low_rate and source != "bump-charge":
                            new_slot["average"] = self.rate_min  # Assume price in min
                        new_slot["cost"] = 0.0
                        new_slot["soc"] = car_soc
                        new_slots.append(new_slot)

                else:
                    car_soc = min(car_soc + kwh_expected, limit)
                    new_slot = {}
                    new_slot["start"] = start_minutes
                    new_slot["end"] = end_minutes
                    new_slot["kwh"] = kwh
                    new_slot["average"] = self.rate_import.get(start_minutes, self.rate_min)
                    if octopus_slot_low_rate and source != "bump-charge":
                        new_slot["average"] = self.rate_min  # Assume price in min
                    new_slot["cost"] = new_slot["average"] * kwh
                    new_slot["soc"] = car_soc
                    new_slots.append(new_slot)
        return new_slots

    def rate_add_io_slots(self, rates, octopus_slots):
        """
        # Add in any planned octopus slots
        """
        octopus_slot_low_rate = self.get_arg("octopus_slot_low_rate", True)
        if octopus_slots:
            # Add in IO slots
            for slot in octopus_slots:
                start_minutes, end_minutes, kwh, source, location = self.decode_octopus_slot(slot, raw=True)

                # Ignore bump-charge slots as their cost won't change
                if source != "bump-charge" and (not location or location == "AT_HOME"):
                    # Round slots to 30 minute boundary
                    start_minutes = int(round(start_minutes / 30, 0) * 30)
                    end_minutes = int(round(end_minutes / 30, 0) * 30)

                    if octopus_slot_low_rate:
                        assumed_price = self.rate_min
                        for minute in range(start_minutes, end_minutes):
                            if minute >= (-96 * 60) and minute < self.forecast_minutes:
                                rates[minute] = assumed_price
                    else:
                        assumed_price = self.rate_import.get(start_minutes, self.rate_min)

                    self.log(
                        "Octopus Intelligent slot at {}-{} assumed price {} amount {} kWh location {} source {} octopus_slot_low_rate {}".format(
                            self.time_abs_str(start_minutes), self.time_abs_str(end_minutes), assumed_price, kwh, location, source, octopus_slot_low_rate
                        )
                    )

        return rates

    def fetch_octopus_rates(self, entity_id, adjust_key=None):
        """
        Fetch the Octopus rates from the sensor

        :param entity_id: The entity_id of the sensor
        :param adjust_key: The key use to find Octopus Intelligent adjusted rates
        """
        data_all = []
        rate_data = {}

        if entity_id:
            # From 9.0.0 of the Octopus plugin the data is split between previous rate, current rate and next rate
            # and the sensor is replaced with an event - try to support the old settings and find the new events

            if self.debug_enable:
                self.log("Fetch Octopus rates from {}".format(entity_id))

            # Previous rates
            if "_current_rate" in entity_id:
                # Try as event
                prev_rate_id = entity_id.replace("_current_rate", "_previous_day_rates").replace("sensor.", "event.")
                data_import = self.get_state_wrapper(entity_id=prev_rate_id, attribute="rates")
                if data_import:
                    data_all += data_import
                else:
                    prev_rate_id = entity_id.replace("_current_rate", "_previous_rate")
                    data_import = self.get_state_wrapper(entity_id=prev_rate_id, attribute="all_rates")
                    if data_import:
                        data_all += data_import
                    else:
                        self.log("Warn: No Octopus data in sensor {} attribute 'all_rates'".format(prev_rate_id))

            # Current rates
            if "_current_rate" in entity_id:
                current_rate_id = entity_id.replace("_current_rate", "_current_day_rates").replace("sensor.", "event.")
            else:
                current_rate_id = entity_id

            data_import = self.get_state_wrapper(entity_id=current_rate_id, attribute="rates") or self.get_state_wrapper(entity_id=current_rate_id, attribute="all_rates") or self.get_state_wrapper(entity_id=current_rate_id, attribute="raw_today")
            if data_import:
                data_all += data_import
            else:
                self.log("Warn: No Octopus data in sensor {} attribute 'all_rates' / 'rates' / 'raw_today'".format(current_rate_id))

            # Next rates
            if "_current_rate" in entity_id:
                next_rate_id = entity_id.replace("_current_rate", "_next_day_rates").replace("sensor.", "event.")
                data_import = self.get_state_wrapper(entity_id=next_rate_id, attribute="rates")
                if data_import:
                    data_all += data_import
                else:
                    next_rate_id = entity_id.replace("_current_rate", "_next_rate")
                    data_import = self.get_state_wrapper(entity_id=next_rate_id, attribute="all_rates")
                    if data_import:
                        data_all += data_import
            else:
                # Nordpool tomorrow
                data_import = self.get_state_wrapper(entity_id=current_rate_id, attribute="raw_tomorrow")
                if data_import:
                    data_all += data_import

        if data_all:
            rate_key = "rate"
            from_key = "from"
            to_key = "to"
            scale = 1.0
            if rate_key not in data_all[0]:
                rate_key = "value_inc_vat"
                from_key = "valid_from"
                to_key = "valid_to"
            if from_key not in data_all[0]:
                from_key = "start"
                to_key = "end"
                scale = 100.0
            if rate_key not in data_all[0]:
                rate_key = "value"
            rate_data = self.minute_data(data_all, self.forecast_days + 1, self.midnight_utc, rate_key, from_key, backwards=False, to_key=to_key, adjust_key=adjust_key, scale=scale)

        return rate_data

    def fetch_octopus_sessions(self):
        """
        Fetch the Octopus saving/free sessions
        """

        # Octopus free session
        octopus_free_slots = []
        if "octopus_free_session" in self.args:
            entity_id = self.get_arg("octopus_free_session", indirect=False)
            if entity_id:
                events = self.get_state_wrapper(entity_id=entity_id, attribute="events")
                if events:
                    for event in events:
                        start = event.get("start", None)
                        end = event.get("end", None)
                        code = event.get("code", None)
                        if start and end and code:
                            start_time = str2time(start)  # reformat the saving session start & end time for improved readability
                            end_time = str2time(end)
                            diff_time = start_time - self.now_utc
                            if abs(diff_time.days) <= 3:
                                self.log("Octopus free events code {} {}-{}".format(code, start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M")))
                            octopus_free_slot = {}
                            octopus_free_slot["start"] = start
                            octopus_free_slot["end"] = end
                            octopus_free_slot["rate"] = 0
                            octopus_free_slots.append(octopus_free_slot)
        # Direct Octopus URL
        if "octopus_free_url" in self.args:
            free_online = self.download_octopus_free(self.get_arg("octopus_free_url", indirect=False))
            octopus_free_slots.extend(free_online)

        # Octopus saving session
        octopus_saving_slots = []
        if "octopus_saving_session" in self.args:
            saving_rate = 200  # Default rate if not reported
            octopoints_per_penny = self.get_arg("octopus_saving_session_octopoints_per_penny", 8)  # Default 8 octopoints per found

            entity_id = self.get_arg("octopus_saving_session", indirect=False)
            if entity_id:
                state = self.get_arg("octopus_saving_session", False)

                joined_events = self.get_state_wrapper(entity_id=entity_id, attribute="joined_events")
                if not joined_events:
                    entity_id = entity_id.replace("binary_sensor.", "event.").replace("_sessions", "_session_events")
                    joined_events = self.get_state_wrapper(entity_id=entity_id, attribute="joined_events")

                available_events = self.get_state_wrapper(entity_id=entity_id, attribute="available_events")
                if available_events:
                    for event in available_events:
                        code = event.get("code", None)  # decode the available events structure for code, start/end time & rate
                        start = event.get("start", None)
                        end = event.get("end", None)
                        start_time = str2time(start)  # reformat the saving session start & end time for improved readability
                        end_time = str2time(end)
                        saving_rate = event.get("octopoints_per_kwh", saving_rate * octopoints_per_penny) / octopoints_per_penny  # Octopoints per pence
                        if code:  # Join the new Octopus saving event and send an alert
                            self.log("Joining Octopus saving event code {} {}-{} at rate {} p/kWh".format(code, start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate))
                            self.call_service_wrapper("octopus_energy/join_octoplus_saving_session_event", event_code=code, entity_id=entity_id)
                            self.call_notify("Predbat: Joined Octopus saving event {}-{}, {} p/kWh".format(start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate))

                if joined_events:
                    for event in joined_events:
                        start = event.get("start", None)
                        end = event.get("end", None)
                        saving_rate = event.get("octopoints_per_kwh", saving_rate * octopoints_per_penny) / octopoints_per_penny  # Octopoints per pence
                        if start and end and saving_rate > 0:
                            # Save the saving slot?
                            try:
                                start_time = str2time(start)
                                end_time = str2time(end)
                                diff_time = start_time - self.now_utc
                                if abs(diff_time.days) <= 3:
                                    self.log("Joined Octopus saving session: {}-{} at rate {} p/kWh state {}".format(start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate, state))

                                    # Save the slot
                                    octopus_saving_slot = {}
                                    octopus_saving_slot["start"] = start
                                    octopus_saving_slot["end"] = end
                                    octopus_saving_slot["rate"] = saving_rate
                                    octopus_saving_slot["state"] = state
                                    octopus_saving_slots.append(octopus_saving_slot)
                            except (ValueError, TypeError):
                                self.log("Warn: Bad start time for joined Octopus saving session: {}-{} at rate {} p/kWh state {}".format(start_time.strftime("%a %d/%m %H:%M"), end_time.strftime("%H:%M"), saving_rate, state))

                    # In saving session that's not reported, assumed 30-minutes
                    if state and not joined_events:
                        octopus_saving_slot = {}
                        octopus_saving_slot["start"] = None
                        octopus_saving_slot["end"] = None
                        octopus_saving_slot["rate"] = saving_rate
                        octopus_saving_slot["state"] = state
                        octopus_saving_slots.append(octopus_saving_slot)
                    if state:
                        self.log("Octopus Saving session is active!")
        return octopus_free_slots, octopus_saving_slots
