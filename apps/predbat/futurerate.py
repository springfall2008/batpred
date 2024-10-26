from datetime import datetime, timedelta
import os
import requests
import json
import pytz
import copy

from config import TIME_FORMAT
TIME_FORMAT_NORD = "%d-%m-%YT%H:%M:%S%z"

class FutureRate:
    def __init__(self, base):
        self.base = base
        self.dp1 = base.dp1
        self.dp2 = base.dp2
        self.record_status = base.record_status
        self.log = base.log
        self.get_arg = base.get_arg
        self.midnight = base.midnight
        self.midnight_utc = base.midnight_utc
        self.minute_data = base.minute_data
        self.forecast_days = base.forecast_days
        self.minutes_now = base.minutes_now
        self.forecast_plan_hours = base.forecast_plan_hours
        self.time_abs_str = base.time_abs_str
        self.futurerate_url_cache = base.futurerate_url_cache

    def futurerate_calibrate(self, real_mdata, mdata, is_import, peak_start_minutes, peak_end_minutes):
        """
        Calibrate nordpool data
        """
        if is_import:
            best_diff_multiply = 2.0
            best_diff_add_peak = 7
            best_diff_add_all = 0
        else:
            best_diff_multiply = 0.95
            best_diff_add_peak = 1.0
            best_diff_add_all = 5.0
        best_diff_diff = 9999999
        vat = 1.05

        if real_mdata:
            for corrolate_multiply in [x / 100 for x in range(90, 120, 5)]:
                diff = 0
                for minute in real_mdata:
                    minute_mod = minute % (24*60)
                    rate_real = real_mdata[minute]
                    rate_nord = mdata.get(minute, None)
                    if minute >= peak_start_minutes and minute_mod < peak_end_minutes:
                        continue
                    if rate_nord is not None:
                        rate_nord *= corrolate_multiply

                        if is_import:
                            rate_nord = min(rate_nord, 95)  # Cap
                        else:
                            rate_nord = max(rate_nord, 0)

                        diff += abs(rate_real / vat - rate_nord)
                if diff <= best_diff_diff:
                    best_diff_diff = diff
                    best_diff_multiply = corrolate_multiply
            
        best_diff_diff = 9999999
        if is_import:
            peak_range = [x / 10.0 for x in range(0, 160, 5)]
            all_range = [0]
        else:
            peak_range = range(0, 8)
            all_range = [x / 10.0 for x in range(0, 20, 2)]

        if real_mdata:
            for corrolate_add in all_range:
                diff = 0
                for minute in real_mdata:
                    minute_mod = minute % (24*60)
                    rate_real = real_mdata[minute]
                    rate_nord = mdata.get(minute, None)
                    if rate_nord is not None:
                        rate_nord *= best_diff_multiply
                        rate_nord += corrolate_add      
                        
                        if is_import:
                            rate_nord = min(rate_nord, 95)  # Cap
                        else:
                            rate_nord = max(rate_nord, 0)
                
                        diff += abs(rate_real / vat - rate_nord)

                if diff <= best_diff_diff:
                    best_diff_diff = diff
                    best_diff_add_all = corrolate_add

            for corrolate_add in peak_range:
                diff = 0
                for minute in real_mdata:
                    minute_mod = minute % (24*60)
                    rate_real = real_mdata[minute]
                    rate_nord = mdata.get(minute, None)
                    if rate_nord is not None:
                        rate_nord *= best_diff_multiply
                        if minute_mod >= peak_start_minutes and minute_mod < peak_end_minutes:
                            rate_nord += corrolate_add
                        rate_nord += best_diff_add_all

                        if is_import:
                            rate_nord = min(rate_nord, 95)  # Cap
                        else:
                            rate_nord = max(rate_nord, 0)

                        diff += abs(rate_real  / vat - rate_nord)

                if diff <= best_diff_diff:
                    best_diff_diff = diff
                    best_diff_add_peak = corrolate_add

        # Perform adjustment
        calib_data = {}
        for minute in mdata:
            minute_mod = minute % (24*60)
            rate_nord = mdata[minute]
            rate_nord *= best_diff_multiply
            if minute_mod >= peak_start_minutes and minute_mod < peak_end_minutes:
                rate_nord += best_diff_add_peak    
            rate_nord += best_diff_add_all
            if is_import:
                rate_nord = min(rate_nord, 95)  # Cap
            else:
                rate_nord = max(rate_nord, 0)

            calib_data[minute] = rate_nord * vat
        
        return calib_data



    def futurerate_analysis_new(self, url_template, rate_import_real, rate_export_real):
        """
        Convert new Futurerate data to minute data
        """
        all_data = {}
        extracted_data = {}
        extracted_keys = []
        array_values = []

        peak_start = datetime.strptime(self.get_arg("futurerate_peak_start", "00:00:00"), "%H:%M:%S")
        peak_end = datetime.strptime(self.get_arg("futurerate_peak_end", "00:00:00"), "%H:%M:%S")
        peak_start_minutes = peak_start.minute + peak_start.hour * 60
        peak_end_minutes = peak_end.minute + peak_end.hour * 60
        if peak_end_minutes < peak_start_minutes:
            peak_end_minutes += 24 * 60

        for day in [0, 1]:
            url = url_template.replace("DATE", (datetime.now() + timedelta(days=day)).strftime("%Y-%m-%d"))
            try:
                pdata = self.download_futurerate_data(url)
            except (ValueError, TypeError):
                return {}, {}
            if not pdata and day == 0:
                self.log("Warn: Error downloading futurerate data from URL {}, no data".format(url))
                self.record_status("Warn: Error downloading futurerate data from cloud, no data", debug=url, had_errors=True)
                return {}, {}

            if not all_data:
                all_data = copy.deepcopy(pdata)
            else:
                if 'multiAreaEntries' in pdata:
                    all_data["multiAreaEntries"].extend(pdata["multiAreaEntries"])
        
        if not all_data or ("multiAreaEntries" not in all_data):
            self.log("Warn: Error downloading futurerate data from URL {}, no multiAreaEntries".format(url))
            self.record_status("Warn: Error downloading futurerate data from cloud, no multiAreaEntries", debug=url, had_errors=True)
            return {}, {}

        for entry in all_data["multiAreaEntries"]:
            deliveryStart = entry["deliveryStart"]
            deliveryEnd = entry["deliveryEnd"]
            entryPerArea = entry["entryPerArea"]
            areaPrice = 0
            for area in entryPerArea:
                areaPrice = entryPerArea[area]

            time_date_start = datetime.strptime(deliveryStart, TIME_FORMAT)
            time_date_end = datetime.strptime(deliveryEnd, TIME_FORMAT)
            if time_date_end < time_date_start:
                time_date_end += timedelta(days=1)
            delta_start = time_date_start - self.midnight_utc
            delta_end = time_date_end - self.midnight_utc

            minutes_start = delta_start.seconds / 60
            minutes_end = delta_end.seconds / 60
            if minutes_end < minutes_start:
                minutes_end += 24 * 60
            
            # Convert to pence with Agile formula, starts in pounds per Megawatt hour
            rate_import = (areaPrice / 10) * 2.0
            rate_export = (areaPrice / 10) * 1.0

            item = {}
            item["from"] = time_date_start.strftime(TIME_FORMAT)
            item["to"] = time_date_end.strftime(TIME_FORMAT)
            item["rate_import"] = self.dp2(rate_import)
            item["rate_export"] = self.dp2(rate_export)
            self.log("Futurerate: {} {} to {} import {} export {}".format(item["from"], deliveryStart, item["to"], item["rate_import"], item["rate_export"]))

            if time_date_start not in extracted_keys:
                extracted_keys.append(time_date_start)
                extracted_data[time_date_start] = item
            else:
                self.log("Warn: Duplicate key {} in extracted_keys".format(time_date_start))

        if extracted_keys:
            extracted_keys.sort()
            for key in extracted_keys:
                array_values.append(extracted_data[key])
            self.log("Loaded {} datapoints of futurerate analysis".format(len(extracted_keys)))
            mdata_import = self.minute_data(array_values, self.forecast_days + 1, self.midnight_utc, "rate_import", "from", backwards=False, to_key="to")
            mdata_export = self.minute_data(array_values, self.forecast_days + 1, self.midnight_utc, "rate_export", "from", backwards=False, to_key="to")

        self.log("Info: Calibrating Nordpool data...")
        mdata_import = self.futurerate_calibrate(rate_import_real, mdata_import, is_import=True, peak_start_minutes=peak_start_minutes, peak_end_minutes=peak_end_minutes)
        mdata_export = self.futurerate_calibrate(rate_export_real, mdata_export, is_import=False, peak_start_minutes=peak_start_minutes, peak_end_minutes=peak_end_minutes)

        future_data = []
        minute_now_hour = int(self.minutes_now / 60) * 60
        for minute in range(0, self.forecast_plan_hours * 60 + minute_now_hour, 60):
            if mdata_import.get(minute) or mdata_export.get(minute):
                future_data.append("{} => {} / {}".format(self.time_abs_str(minute), mdata_import.get(minute), mdata_export.get(minute)))

        self.log("Predicted future rates: {}".format(future_data))
        return mdata_import, mdata_export
    
    def futurerate_analysis(self, rate_import_real, rate_export_real):
        """
        Analyse futurerate energy data
        """

        url = None
        if "futurerate_url" in self.base.args:
            url = self.base.args["futurerate_url"]
        if not url:
            return {}, {}

        self.log("Fetching futurerate data from {}".format(url))

        if 'DATE' in url:
            return self.futurerate_analysis_new(url, rate_import_real, rate_export_real)
        else:
            print("Warning: Old futurerate URL, you must update this in apps.yaml")
            return {}, {}


    def download_futurerate_data(self, url):
        """
        Download futurerate data directly from a URL or return from cache if recent
        Retry 3 times and then throw error
        """

        # Check the cache first
        now = datetime.now()
        if url in self.futurerate_url_cache:
            stamp = self.futurerate_url_cache[url]["stamp"]
            pdata = self.futurerate_url_cache[url]["data"]
            update_time_since_midnight = stamp - self.midnight
            now_since_midnight = now - self.midnight
            age = now - stamp
            needs_update = False

            # Update if last data was yesterday
            if update_time_since_midnight.seconds < 0:
                needs_update = True

            # data updates at 11am CET so update every 30 minutes during this period
            if now_since_midnight.seconds > (9.5 * 60 * 60) and now_since_midnight.seconds < (11 * 60 * 60) and age.seconds > (0.5 * 60 * 60):
                needs_update = True
            if age.seconds > (12 * 60 * 60):
                needs_update = True

            if not needs_update:
                self.log("Return cached futurerate data for {} age {} minutes".format(url, self.dp1(age.seconds / 60)))
                return pdata

        # Retry up to 3 minutes
        for retry in range(3):
            pdata = self.download_futurerate_data_func(url)
            if pdata:
                break

        # Download failed?
        if not pdata:
            self.log("Warn: Error downloading futurerate data from URL {}".format(url))
            self.record_status("Warn: Error downloading futurerate data from cloud", debug=url, had_errors=True)
            if url in self.futurerate_url_cache:
                pdata = self.futurerate_url_cache[url]["data"]
                return pdata
            else:
                raise ValueError

        if pdata == "empty":
            pdata = {}
        
        # Cache New Octopus data
        self.futurerate_url_cache[url] = {}
        self.futurerate_url_cache[url]["stamp"] = now
        self.futurerate_url_cache[url]["data"] = pdata
        return pdata

    def download_futurerate_data_func(self, url):
        try:
            r = requests.get(url)
        except Exception as e:
            self.log("Warn: Error downloading futurerate data from URL {}, request exception {}".format(url, e))
            self.record_status("Warn: Error downloading futurerate data from cloud", debug=url, had_errors=True)
            return {}
        
        if r.status_code in [204]:
            return "empty"

        if r.status_code not in [200, 201]:
            self.log("Warn: Error downloading futurerate data from URL {}, code {}".format(url, r.status_code))
            self.record_status("Warn: Error downloading futurerate data from cloud", debug=url, had_errors=True)
            return {}
        
        try:
            struct = json.loads(r.text)
        except requests.exceptions.JSONDecodeError:
            self.log("Warn: Error downloading futurerate data from URL {}".format(url))
            self.record_status("Warn: Error downloading futurerate data from cloud", debug=url, had_errors=True)
            return {}
        
        return struct
