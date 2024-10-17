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

    def futurerate_analysis_new(self, url_template):
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

        peak_premium_import = self.get_arg("futurerate_peak_premium_import", 0)
        peak_premium_export = self.get_arg("futurerate_peak_premium_export", 0)

        for day in [0, 1]:
            url = url_template.replace("DATE", (datetime.now() + timedelta(days=day)).strftime("%Y-%m-%d"))
            try:
                pdata = self.download_futurerate_data(url)
            except (ValueError, TypeError):
                return {}, {}
            if not pdata and day == 0:
                self.log("Warn: No data for nordpool today")
                return {}, {}

            if not all_data:
                all_data = copy.deepcopy(pdata)
            else:
                if "multiAreaEntries" in pdata:
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
            rate_import = (areaPrice / 10) * 2.2
            rate_export = (areaPrice / 10) * 0.95
            if minutes_start >= peak_start_minutes and minutes_end <= peak_end_minutes:
                rate_import += peak_premium_import
                rate_export += peak_premium_export
            rate_import = min(rate_import, 95)  # Cap
            rate_export = max(rate_export, 0)  # Cap
            rate_import = rate_import * 1.05  # Vat only on import

            item = {}
            item["from"] = time_date_start.strftime(TIME_FORMAT)
            item["to"] = time_date_end.strftime(TIME_FORMAT)
            item["rate_import"] = self.dp2(rate_import)
            item["rate_export"] = self.dp2(rate_export)

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

        future_data = []
        minute_now_hour = int(self.minutes_now / 60) * 60
        for minute in range(minute_now_hour, self.forecast_plan_hours * 60 + minute_now_hour, 60):
            if mdata_import.get(minute) or mdata_export.get(minute):
                future_data.append("{} => {} / {}".format(self.time_abs_str(minute), mdata_import.get(minute), mdata_export.get(minute)))

        self.log("Predicted future rates: {}".format(future_data))
        return mdata_import, mdata_export

    def futurerate_analysis(self):
        """
        Analyse futurerate energy data
        """

        url = None
        if "futurerate_url" in self.base.args:
            url = self.base.args["futurerate_url"]
        if not url:
            return {}, {}

        self.log("Fetching futurerate data from {}".format(url))

        if "DATE" in url:
            return self.futurerate_analysis_new(url)
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

        if pdata == "empty":
            return {}

        # Download failed?
        if not pdata:
            self.log("Warn: Error downloading futurerate data from URL {}".format(url))
            self.record_status("Warn: Error downloading futurerate data from cloud", debug=url, had_errors=True)
            if url in self.futurerate_url_cache:
                pdata = self.futurerate_url_cache[url]["data"]
                return pdata
            else:
                raise ValueError

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
