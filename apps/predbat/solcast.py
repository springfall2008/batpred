# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import hashlib
import json
import os
import requests
import traceback
import pytz
from datetime import datetime, timedelta
from config import TIME_FORMAT, TIME_FORMAT_SOLCAST
from utils import dp1, dp2

"""
Solcast class deals with fetching solar predictions, processing the data and publishing the results.
"""


class Solcast:
    def cache_get_url(self, url, params, max_age=8 * 60):
        # Get data from cache
        age_minutes = 0
        data = None
        hash = url + "_" + hashlib.md5(str(params).encode()).hexdigest()
        hash = hash.replace("/", "_")
        hash = hash.replace(":", "_")
        hash = hash.replace("?", "a")
        hash = hash.replace("&", "b")
        hash = hash.replace("*", "c")
        cache_path = self.config_root + "/cache"
        if not os.path.exists(cache_path):
            os.makedirs(cache_path)

        cache_filename = cache_path + "/" + hash + ".json"
        if os.path.exists(cache_filename):
            timestamp = os.path.getmtime(cache_filename)
            age = datetime.now() - datetime.fromtimestamp(timestamp)
            age_minutes = (age.seconds + age.days * 24 * 60) / 60

            # Read cache data
            with open(cache_filename) as f:
                data = json.load(f)

                if age_minutes < max_age:
                    self.log("Return cached data for {} age {} minutes".format(url, dp1(age_minutes)))
                    return data

        # Perform fetch
        self.log("Fetching {}".format(url))
        try:
            r = requests.get(url, params=params)
        except requests.exceptions.ConnectionError as e:
            self.log("Warn: Error downloading data from URL {}, error {}".format(url, e))
            return data

        if r.status_code not in [200, 201]:
            self.log("Warn: Error downloading data from url {}, code {}".format(url, r.status_code))
        else:
            try:
                data = r.json()
            except requests.exceptions.JSONDecodeError as e:
                self.log("Warn: Error downloading data from URL {}, error {} code {} data was {}".format(url, e, r.status_code, r.text))
                if data:
                    self.log("Warn: Error downloading data from URL {}, using cached data age {} minutes".format(url, dp1(age_minutes)))
                else:
                    self.log("Warn: Error downloading data from URL {}, no cached data".format(url))

        # Store data in cache
        if data:
            self.log("Writing cache data for {} to cache file {}".format(url, cache_filename))
            with open(cache_filename, "w") as f:
                json.dump(data, f)
        return data

    def download_solcast_data(self):
        """
        Download solcast data directly from a URL or return from cache if recent.
        """
        self.solcast_api_limit = 0
        self.solcast_api_used = 0
        cache_path = self.config_root + "/cache"
        cache_path_p = self.config_root_p + "/cache"

        host = self.args.get("solcast_host", None)
        api_keys = self.args.get("solcast_api_key", None)
        if not api_keys or not host:
            self.log("Warn: Solcast API key or host not set")
            return None

        # Remove trailing '/' from host URL if necessary to prevent pathnames becoming e.g. https://api.solcast.com.au//rooftop_sites
        if host[-1] == "/":
            host = host[0:-1]

        self.solcast_data = {}
        cache_file = cache_path + "/solcast.json"
        cache_file_p = cache_path_p + "/solcast.json"

        if os.path.exists(cache_file):
            try:
                with open(cache_file) as f:
                    self.solcast_data = json.load(f)
            except Exception as e:
                self.log("Warn: Error loading Solcast cache file {}, error {}".format(cache_file_p, e))
                self.log("Warn: " + traceback.format_exc())
                os.remove(cache_file)

        if isinstance(api_keys, str):
            api_keys = [api_keys]

        period_data = {}
        max_age = self.get_arg("solcast_poll_hours", 8.0) * 60

        for api_key in api_keys:
            params = {"format": "json", "api_key": api_key.strip()}

            # API Limit no longer works - 15/8/24
            # wait for Solcast to provide new API
            #
            # url = f"{host}/json/reply/GetUserUsageAllowance"
            # data = self.cache_get_url(url, params, max_age=0)
            # if not data:
            #    self.log("Warn: Solcast, could not access usage data, check your Solcast cloud settings")
            # else:
            #    self.solcast_api_limit += data.get("daily_limit", None)
            #    self.solcast_api_used += data.get("daily_limit_consumed", None)
            #    self.log("Solcast API limit {} used {}".format(self.solcast_api_limit, self.solcast_api_used))

            site_config = self.get_arg("solcast_sites", [])
            if site_config:
                sites = []
                for site in site_config:
                    sites.append({"resource_id": site})
            else:
                url = f"{host}/rooftop_sites"
                data = self.cache_get_url(url, params, max_age=max_age)
                if not data:
                    self.log("Warn: Solcast sites could not be downloaded, try setting solcast_sites in apps.yaml instead")
                    continue
                sites = data.get("sites", [])

            for site in sites:
                resource_id = site.get("resource_id", None)
                if resource_id:
                    self.log("Fetch data for resource id {}".format(resource_id))

                    params = {"format": "json", "api_key": api_key.strip(), "hours": 168}
                    url = f"{host}/rooftop_sites/{resource_id}/forecasts"
                    data = self.cache_get_url(url, params, max_age=max_age)
                    if not data:
                        self.log("Warn: Solcast forecast data for site {} could not be downloaded, check your Solcast cloud settings".format(site))
                        continue
                    forecasts = data.get("forecasts", [])

                    for forecast in forecasts:
                        period_end = forecast.get("period_end", None)
                        if period_end:
                            period_end_stamp = datetime.strptime(period_end, TIME_FORMAT_SOLCAST)
                            period_end_stamp.replace(tzinfo=pytz.utc)
                            period_period = forecast.get("period", "PT30M")
                            period_minutes = int(period_period[2:-1])
                            period_start_stamp = period_end_stamp - timedelta(minutes=period_minutes)
                            pv50 = forecast.get("pv_estimate", 0) / 60 * period_minutes
                            pv10 = forecast.get("pv_estimate10", forecast.get("pv_estimate", 0)) / 60 * period_minutes
                            pv90 = forecast.get("pv_estimate90", forecast.get("pv_estimate", 0)) / 60 * period_minutes

                            data_item = {"period_start": period_start_stamp.strftime(TIME_FORMAT), "pv_estimate": pv50, "pv_estimate10": pv10, "pv_estimate90": pv90}
                            if period_start_stamp in period_data:
                                period_data[period_start_stamp]["pv_estimate"] += pv50
                                period_data[period_start_stamp]["pv_estimate10"] += pv10
                                period_data[period_start_stamp]["pv_estimate90"] += pv90
                            else:
                                period_data[period_start_stamp] = data_item

        # Merge the new data into the cached data
        new_data = {}
        for key in period_data:
            self.solcast_data[key.strftime(TIME_FORMAT)] = period_data[key]

        # Prune old data from the cache
        for key_txt in self.solcast_data:
            key = datetime.strptime(key_txt, TIME_FORMAT)
            if key >= self.midnight_utc:
                new_data[key_txt] = self.solcast_data[key_txt]
        self.solcast_data = new_data

        # Save to cache file
        with open(cache_file, "w") as f:
            json.dump(self.solcast_data, f)

        # Fetch the final cached data as timestamps
        period_data = {}
        for key_txt in self.solcast_data:
            key = datetime.strptime(key_txt, TIME_FORMAT)
            period_data[key] = self.solcast_data[key_txt]

        # Sort data and return
        sorted_data = []
        if period_data:
            period_keys = list(period_data.keys())
            period_keys.sort()
            for key in period_keys:
                sorted_data.append(period_data[key])

        self.log("Solcast returned {} data points".format(len(sorted_data)))
        return sorted_data

    def fetch_pv_datapoints(self, argname):
        """
        Get some solcast data from argname argument
        """
        data = []
        total_data = 0
        total_sensor = 0

        if argname in self.args:
            # Found out if detailedForecast is present or not, then set the attribute name
            # in newer solcast plugins only forecast is used
            attribute = "detailedForecast"
            entity_id = self.get_arg(argname, None, indirect=False)
            if entity_id:
                result = self.get_state_wrapper(entity_id=entity_id, attribute=attribute)
                if not result:
                    attribute = "forecast"
                try:
                    data = self.get_state_wrapper(entity_id=self.get_arg(argname, indirect=False), attribute=attribute)
                except (ValueError, TypeError):
                    self.log("Warn: Unable to fetch solar forecast data from sensor {} check your setting of {}".format(self.get_arg(argname, indirect=False), argname))
                    self.record_status("Error: {} not be set correctly, check apps.yaml", debug=self.get_arg(argname, indirect=False), had_errors=True)

            # Solcast new vs old version
            # check the total vs the sum of 30 minute slots and work out scale factor
            if data:
                for entry in data:
                    total_data += entry["pv_estimate"]
                total_data = dp2(total_data)
                total_sensor = dp2(self.get_arg(argname, 1.0))
        return data, total_data, total_sensor

    def publish_pv_stats(self, pv_forecast_data, divide_by, period):
        """
        Publish some PV stats
        """

        total_left_today = 0
        total_left_today10 = 0
        total_left_today90 = 0
        forecast_day = {}
        total_day = {}
        total_day10 = {}
        total_day90 = {}
        days = 0

        days = min(days, 7)
        for day in range(days):
            total_day[day] = 0
            total_day10[day] = 0
            total_day90[day] = 0
            forecast_day[day] = []

        midnight_today = self.midnight_utc
        now = self.now_utc

        power_scale = 60 / period  # Scale kwh to power
        power_now = 0
        power_now10 = 0
        power_now90 = 0

        for entry in pv_forecast_data:
            if "period_start" not in entry:
                continue
            try:
                this_point = datetime.strptime(entry["period_start"], TIME_FORMAT)
            except (ValueError, TypeError):
                continue

            if this_point >= midnight_today:
                day = (this_point - midnight_today).days
                if day not in total_day:
                    total_day[day] = 0
                    total_day10[day] = 0
                    total_day90[day] = 0
                    forecast_day[day] = []
                    days = max(days, day + 1)

                pv_estimate = entry.get("pv_estimate", 0)
                pv_estimate10 = entry.get("pv_estimate10", pv_estimate)
                pv_estimate90 = entry.get("pv_estimate90", pv_estimate)

                pv_estimate /= divide_by
                pv_estimate10 /= divide_by
                pv_estimate90 /= divide_by

                total_day[day] += pv_estimate
                total_day10[day] += pv_estimate10
                total_day90[day] += pv_estimate90

                if day == 0 and this_point >= now:
                    total_left_today += pv_estimate
                    total_left_today10 += pv_estimate10
                    total_left_today90 += pv_estimate90

                if this_point <= now and (this_point + timedelta(minutes=30)) > now:
                    power_now = pv_estimate * power_scale
                    power_now10 = pv_estimate10 * power_scale
                    power_now90 = pv_estimate90 * power_scale

                    # Add this slot into the total left today but scaled for the time since this point
                    if this_point < now:
                        left_this_slot_scale = (now - this_point).total_seconds() / 3600.0
                        total_left_today += pv_estimate * power_scale * left_this_slot_scale
                        total_left_today10 += pv_estimate10 * power_scale * left_this_slot_scale
                        total_left_today90 += pv_estimate90 * power_scale * left_this_slot_scale

                fentry = {
                    "period_start": entry["period_start"],
                    "pv_estimate": dp2(pv_estimate * power_scale),
                    "pv_estimate10": dp2(pv_estimate10 * power_scale),
                    "pv_estimate90": dp2(pv_estimate90 * power_scale),
                }
                forecast_day[day].append(fentry)

        days = min(days, 7)
        for day in range(days):
            if day == 0:
                self.log(
                    "PV Forecast for today is {} ({} 10% {} 90%) kWh and left today is {} ({} 10% {} 90%) kWh".format(
                        dp2(total_day[day]),
                        dp2(total_day10[day]),
                        dp2(total_day90[day]),
                        dp2(total_left_today),
                        dp2(total_left_today10),
                        dp2(total_left_today90),
                    )
                )
                self.dashboard_item(
                    "sensor." + self.prefix + "_pv_today",
                    state=dp2(total_day[day]),
                    attributes={
                        "friendly_name": "PV Forecast Today",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:solar-power",
                        "device_class": "energy",
                        "total": dp2(total_day[day]),
                        "total10": dp2(total_day10[day]),
                        "total90": dp2(total_day90[day]),
                        "remaining": dp2(total_left_today),
                        "remaining10": dp2(total_left_today10),
                        "remaining90": dp2(total_left_today90),
                        "detailedForecast": forecast_day[day],
                        "api_limit": self.solcast_api_limit,
                        "api_used": self.solcast_api_used,
                    },
                )
                self.dashboard_item(
                    "sensor." + self.prefix + "_pv_forecast_h0",
                    state=dp2(power_now),
                    attributes={
                        "friendly_name": "PV Forecast Now",
                        "state_class": "measurement",
                        "unit_of_measurement": "W",
                        "icon": "mdi:solar-power",
                        "device_class": "power",
                        "now10": dp2(power_now10),
                        "now90": dp2(power_now90),
                    },
                )
            else:
                day_name = "tomorrow" if day == 1 else "d{}".format(day)
                day_name_long = day_name if day == 1 else "day {}".format(day)
                self.log("PV Forecast for day {} is {} ({} 10% {} 90%) kWh".format(day_name, dp2(total_day[day]), dp2(total_day10[day]), dp2(total_day90[day])))

                self.dashboard_item(
                    "sensor." + self.prefix + "_pv_" + day_name,
                    state=dp2(total_day[day]),
                    attributes={
                        "friendly_name": "PV Forecast " + day_name_long,
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:solar-power",
                        "device_class": "energy",
                        "total": dp2(total_day[day]),
                        "total10": dp2(total_day10[day]),
                        "total90": dp2(total_day90[day]),
                        "detailedForecast": forecast_day[day],
                    },
                )

    def fetch_pv_forecast(self):
        """
        Fetch the PV Forecast data from Solcast
        either via HA or direct to their cloud
        """
        pv_forecast_minute = {}
        pv_forecast_minute10 = {}
        pv_forecast_data = []
        pv_forecast_total_data = 0
        pv_forecast_total_sensor = 0

        if "solcast_host" in self.args:
            self.log("Obtaining solar forecast from Solcast API")
            pv_forecast_data = self.download_solcast_data()
            divide_by = 30.0
        else:
            self.log("Using Solcast integration from inside HA for solar forecast")

            # Fetch data from each sensor
            for argname in ["pv_forecast_today", "pv_forecast_tomorrow", "pv_forecast_d3", "pv_forecast_d4"]:
                data, total_data, total_sensor = self.fetch_pv_datapoints(argname)
                if data:
                    self.log("PV Data for {} total {} kWh".format(argname, total_sensor))
                    pv_forecast_data += data
                    pv_forecast_total_data += total_data
                    pv_forecast_total_sensor += total_sensor

            # Work out data scale factor so it adds up (New Solcast is in kW but old was kWH)
            factor = 1.0
            if pv_forecast_total_data > 0.0 and pv_forecast_total_sensor > 0.0:
                factor = round((pv_forecast_total_data / pv_forecast_total_sensor), 1)
            # We want to divide the data into single minute slots
            divide_by = dp2(30 * factor)

            if factor != 1.0 and factor != 2.0:
                self.log("Warn: PV Forecast data adds up to {} kWh but total sensors add up to {} kWh, this is unexpected and hence data maybe misleading (factor {})".format(pv_forecast_total_data, pv_forecast_total_sensor, factor))

        if pv_forecast_data:
            self.publish_pv_stats(pv_forecast_data, divide_by / 30.0, 30)

            pv_estimate = self.get_arg("pv_estimate", default="")
            if pv_estimate is None:
                pv_estimate = "pv_estimate"
            else:
                pv_estimate = "pv_estimate" + str(pv_estimate)

            pv_forecast_minute = self.minute_data(
                pv_forecast_data,
                self.forecast_days + 1,
                self.midnight_utc,
                pv_estimate,
                "period_start",
                backwards=False,
                divide_by=divide_by,
                scale=self.pv_scaling,
                spreading=30,
            )
            pv_forecast_minute10 = self.minute_data(
                pv_forecast_data,
                self.forecast_days + 1,
                self.midnight_utc,
                "pv_estimate10",
                "period_start",
                backwards=False,
                divide_by=divide_by,
                scale=self.pv_scaling,
                spreading=30,
            )
        else:
            self.log("Warn: No solar data has been configured.")

        return pv_forecast_minute, pv_forecast_minute10
