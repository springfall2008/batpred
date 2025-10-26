# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import asyncio
import hashlib
import json
import os
import requests
import traceback
import pytz
from datetime import datetime, timedelta, timezone
from config import PREDICT_STEP, TIME_FORMAT, TIME_FORMAT_SOLCAST, TIME_FORMAT_FORECAST_SOLAR
from utils import dp1, dp2, dp4, history_attribute_to_minute_data, minute_data, history_attribute, prune_today
import time

"""
Solcast class deals with fetching solar predictions, processing the data and publishing the results.
"""


class SolarAPI:
    """
    SolarAPI is responsible for managing and aggregating solar forecast data from multiple sources,
    including Solcast, Forecast.Solar, and direct sensor inputs. It periodically fetches, processes,
    and publishes solar production forecasts for use by the home battery system. SolarAPI operates
    as an asynchronous background component, ensuring up-to-date solar predictions are available
    for system optimization and decision-making.
    """

    def __init__(self, prefix, solcast_host, solcast_api_key, solcast_sites, solcast_poll_hours, forecast_solar, forecast_solar_max_age, pv_forecast_today, pv_forecast_tomorrow, pv_forecast_d3, pv_forecast_d4, pv_scaling, base):
        self.prefix = prefix
        self.solcast_host = solcast_host
        self.solcast_api_key = solcast_api_key
        self.solcast_sites = solcast_sites
        self.solcast_poll_hours = solcast_poll_hours
        self.forecast_solar = forecast_solar
        self.forecast_solar_max_age = forecast_solar_max_age
        self.pv_forecast_today = pv_forecast_today
        self.pv_forecast_tomorrow = pv_forecast_tomorrow
        self.pv_forecast_d3 = pv_forecast_d3
        self.pv_forecast_d4 = pv_forecast_d4
        self.pv_scaling = pv_scaling
        self.base = base
        self.log = base.log
        self.api_started = False
        self.api_stop = False
        self.last_success_timestamp = None
        self.solcast_requests_total = 0
        self.solcast_failures_total = 0
        self.forecast_solar_requests_total = 0
        self.forecast_solar_failures_total = 0
        self.config_root = base.config_root
        self.forecast_days = 4

    async def start(self):
        """
        Start the Solar API
        """
        seconds = 0
        while not self.api_stop:
            if seconds % (30 * 60) == 0:  # Every 30 minutes
                try:
                    self.fetch_pv_forecast()
                except Exception as e:
                    self.log("Error: Solcast API fetch_pv_forecast exception {}".format(e))
                    self.log("Error: " + traceback.format_exc())

            if not self.api_started:
                self.api_started = True
                self.log("SolarAPI started")
            await asyncio.sleep(10)
            seconds += 10

        self.log("Solar API stopped")

    async def stop(self):
        """
        Stop the Solar API
        """
        self.api_stop = True

    def wait_api_started(self):
        """
        Wait for the API to start
        """
        self.log("SolarAPI: Waiting for API to start")
        count = 0
        while not self.api_started and count < 240:
            time.sleep(1)
            count += 1
        if not self.api_started:
            self.log("Warn: SolarAPI: Failed to start")
            return False
        return True

    def is_alive(self):
        """
        Check if the API is alive
        """
        return self.api_started

    def last_updated_time(self):
        """
        Get the last successful update time
        """
        return self.last_success_timestamp

    def cache_get_url(self, url, params, max_age=8 * 60):
        # Check if this is a Solcast API call for metrics tracking
        is_solcast_api = "solcast.com" in url.lower() or "api.solcast" in url.lower()
        is_forecast_solar_api = "forecast.solar" in url.lower()

        # Increment request counter for Solcast API calls
        if is_solcast_api:
            self.solcast_requests_total += 1

        # Increment request counter for forecast.solar API calls
        if is_forecast_solar_api:
            self.forecast_solar_requests_total += 1

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
            if is_solcast_api:
                self.solcast_failures_total += 1
            if is_forecast_solar_api:
                self.forecast_solar_failures_total += 1
            return data

        if r.status_code not in [200, 201]:
            self.log("Warn: Error downloading data from url {}, code {}".format(url, r.status_code))
            if is_solcast_api:
                self.solcast_failures_total += 1
            if is_forecast_solar_api:
                self.forecast_solar_failures_total += 1
        else:
            try:
                data = r.json()
                if is_solcast_api:
                    self.solcast_last_success_timestamp = datetime.now(timezone.utc)
                if is_forecast_solar_api:
                    self.forecast_solar_last_success_timestamp = datetime.now(timezone.utc)
            except requests.exceptions.JSONDecodeError as e:
                self.log("Warn: Error downloading data from URL {}, error {} code {} data was {}".format(url, e, r.status_code, r.text))
                if is_solcast_api:
                    self.solcast_failures_total += 1
                if is_forecast_solar_api:
                    self.forecast_solar_failures_total += 1
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

    URL_FREE = "https://api.forecast.solar/estimate/{lat}/{lon}/{dec}/{az}/{kwp}"
    URL_PERSONAL = "https://api.forecast.solar/{api_key}/estimate/{lat}/{lon}/{dec}/{az}/{kwp}"

    def convert_azimuth(self, az):
        """
        Convert azimuth from solcast format to forecast solar format
        solcast format is        0 = North, -90 = East, 90 = West, 180 = South
        forecast solar format is 0 = South, -90 = East, 90 = West, 180 = North
        """
        if az >= 0:
            az = 180 - az
        else:
            az = -180 - az

        return az

    def download_forecast_solar_data(self):
        """
        Download forecast.solar data directly from a URL or return from cache if recent.
        """
        cache_path = self.config_root + "/cache"

        self.forecast_solar_data = {}
        cache_file = cache_path + "/forecast_solar.json"

        if os.path.exists(cache_file):
            try:
                with open(cache_file) as f:
                    self.forecast_solar_data = json.load(f)
            except Exception as e:
                self.log("Warn: Error loading forecast.solar cache file {}, error {}".format(cache_file, e))
                self.log("Warn: " + traceback.format_exc())
                os.remove(cache_file)

        configs = self.forecast_solar
        if configs is None:
            raise ValueError("SolarAPI: No forecast solar configurations found")

        if not isinstance(configs, list):
            configs = [configs]

        period_data = {}
        max_kwh = 0
        for config in configs:
            lat = config.get("latitude", 51.5072)
            lon = config.get("longitude", -0.1276)
            postcode = config.get("postcode", None)
            dec = config.get("declination", 45.0)
            az = config.get("azimuth", 45.0)
            az = self.convert_azimuth(az)  # Convert azimuth to degrees if needed
            kwp = config.get("kwp", 3.0)
            efficiency = config.get("efficiency", 0.95)
            api_key = config.get("api_key", None)

            max_kwh += kwp * efficiency  # Total kWh for this configuration

            if postcode:
                result = self.cache_get_url("https://api.postcodes.io/postcodes/{}".format(postcode), params={}, max_age=24 * 60 * 30)  # Cache postcode data for 30 days
                result = result.get("result", {})
                if "longitude" not in result or "latitude" not in result:
                    self.log("Warn: Postcode {} could not be resolved to latitude and longitude, using default".format(postcode))
                else:
                    lon = result.get("longitude", lon)
                    lat = result.get("latitude", lat)
                    self.log("Postcode {} resolved to latitude {} longitude {}".format(postcode, lat, lon))

            if api_key:
                url = self.URL_PERSONAL
                url = url.format(lat=lat, lon=lon, dec=dec, az=az, kwp=kwp, api_key=api_key)
                days_data = config.get("days", 3)
            else:
                url = self.URL_FREE
                url = url.format(lat=lat, lon=lon, dec=dec, az=az, kwp=kwp)
                days_data = config.get("days", 2)

            data = self.cache_get_url(url, params={}, max_age=self.forecast_solar_max_age * 60)
            watts = data.get("result", {}).get("watt_hours_period", {})
            info = data.get("message", {}).get("info", {})
            if not watts or not info:
                self.log("Warn: Forecast Solar data for lat {} lon {} could not be downloaded, check your Forecast Solar cloud settings, got {}".format(lat, lon, data))
                continue

            current_time = info.get("time", None)
            current_time_stamp = datetime.strptime(current_time, TIME_FORMAT)
            current_time_offset = current_time_stamp.utcoffset()

            period_start_stamp = None
            forecast_watt_data = {}
            for period_end in watts:
                period_end_stamp = datetime.strptime(period_end, TIME_FORMAT_FORECAST_SOLAR)
                # Convert period_end_stamp to a offset aware time using current_time_offset as the timezone
                period_end_stamp = period_end_stamp.replace(tzinfo=pytz.utc) - current_time_offset
                pv50 = watts[period_end] * efficiency  # Apply efficiency to the watt hours
                if period_start_stamp:
                    if period_end_stamp - period_start_stamp > timedelta(minutes=60):
                        period_start_stamp = None
                if period_start_stamp is None:
                    period_start_stamp = period_end_stamp.replace(minute=0, second=0, microsecond=0)  # Start at the beginning of the hour
                    if period_start_stamp == period_end_stamp:
                        period_start_stamp = period_start_stamp - timedelta(minutes=60)
                minutes_start = (period_start_stamp - self.midnight_utc).total_seconds() / 60
                minutes_end = (period_end_stamp - self.midnight_utc).total_seconds() / 60
                duration = (minutes_end - minutes_start) / 60.0
                for minute in range(int(minutes_start), int(minutes_end) + 1):
                    forecast_watt_data[minute] = pv50 / duration

                period_start_stamp = period_end_stamp

            for minute in range(0, days_data * 24 * 60, 30):
                pv50 = 0
                for offset in range(0, 30, 1):
                    pv50 += dp4(forecast_watt_data.get(minute + offset, 0) / 1000.0)
                pv50 /= 30
                period_start_stamp = self.midnight_utc.replace(tzinfo=pytz.utc) + timedelta(minutes=minute)
                data_item = {"period_start": period_start_stamp.strftime(TIME_FORMAT), "pv_estimate": pv50}
                if period_start_stamp in period_data:
                    period_data[period_start_stamp]["pv_estimate"] += pv50
                else:
                    period_data[period_start_stamp] = data_item

        # Merge the new data into the cached data
        new_data = {}
        for key in period_data:
            self.forecast_solar_data[key.strftime(TIME_FORMAT)] = period_data[key]

        # Prune old data from the cache
        for key_txt in self.forecast_solar_data:
            key = datetime.strptime(key_txt, TIME_FORMAT)
            if key >= self.midnight_utc:
                new_data[key_txt] = self.forecast_solar_data[key_txt]
        self.forecast_solar_data = new_data

        # Save to cache file
        with open(cache_file, "w") as f:
            json.dump(self.forecast_solar_data, f)

        # Fetch the final cached data as timestamps
        period_data = {}
        for key_txt in self.forecast_solar_data:
            key = datetime.strptime(key_txt, TIME_FORMAT)
            period_data[key] = self.forecast_solar_data[key_txt]

        # Sort data and return
        sorted_data = []
        if period_data:
            period_keys = list(period_data.keys())
            period_keys.sort()
            for key in period_keys:
                sorted_data.append(period_data[key])

        self.log("Forecast solar returned {} data points".format(len(sorted_data)))
        return sorted_data, max_kwh

    def download_solcast_data(self):
        """
        Download solcast data directly from a URL or return from cache if recent.
        """
        cache_path = self.config_root + "/cache"

        host = self.solcast_host
        api_keys = self.solcast_api_key
        if not api_keys or not host:
            self.log("Warn: Solcast API key or host not set")
            return None

        # Remove trailing '/' from host URL if necessary to prevent pathnames becoming e.g. https://api.solcast.com.au//rooftop_sites
        if host[-1] == "/":
            host = host[0:-1]

        self.solcast_data = {}
        cache_file = cache_path + "/solcast.json"

        if os.path.exists(cache_file):
            try:
                with open(cache_file) as f:
                    self.solcast_data = json.load(f)
            except Exception as e:
                self.log("Warn: Error loading Solcast cache file {}, error {}".format(cache_file, e))
                self.log("Warn: " + traceback.format_exc())
                os.remove(cache_file)

        if isinstance(api_keys, str):
            api_keys = [api_keys]

        period_data = {}
        max_age = self.solcast_poll_hours * 60

        for api_key in api_keys:
            params = {"format": "json", "api_key": api_key.strip()}

            site_config = self.solcast_sites
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

    def fetch_pv_datapoints(self, argname, entity_id):
        """
        Get some solcast data from argname argument
        """
        data = []
        total_data = 0
        total_sensor = 0

        if entity_id:
            # Found out if detailedForecast is present or not, then set the attribute name
            # in newer solcast plugins only forecast is used
            attribute = "detailedForecast"
            if entity_id:
                result = self.base.get_state_wrapper(entity_id=entity_id, attribute=attribute)
                if not result:
                    attribute = "forecast"
                try:
                    data = self.base.get_state_wrapper(entity_id=entity_id, attribute=attribute)
                except (ValueError, TypeError):
                    self.log("Warn: Unable to fetch solar forecast data from sensor {} check your setting of {}".format(entity_id, argname))

            # Solcast new vs old version
            # check the total vs the sum of 30 minute slots and work out scale factor
            if data:
                for entry in data:
                    total_data += entry["pv_estimate"]
                total_data = dp2(total_data)
                total_sensor = dp2(self.base.get_state_wrapper(entity_id=entity_id, default=1.0))
        return data, total_data, total_sensor

    def publish_pv_stats(self, pv_forecast_data, divide_by, period):
        """
        Publish some PV stats
        """

        total_left_today = 0
        total_left_today10 = 0
        total_left_today90 = 0
        total_left_todayCL = 0
        forecast_day = {}
        total_day = {}
        total_day10 = {}
        total_day90 = {}
        total_dayCL = {}
        days = 0

        days = min(days, 7)
        for day in range(days):
            total_day[day] = 0
            total_day10[day] = 0
            total_day90[day] = 0
            total_dayCL[day] = 0
            forecast_day[day] = []

        midnight_today = self.midnight_utc
        now = self.now_utc

        power_scale = 60 / period  # Scale kwh to power
        power_now = 0
        power_now10 = 0
        power_now90 = 0
        power_nowCL = 0

        point_gap = 30
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
                    total_dayCL[day] = 0
                    forecast_day[day] = []
                    days = max(days, day + 1)

                pv_estimate = entry.get("pv_estimate", 0)
                pv_estimate10 = entry.get("pv_estimate10", pv_estimate)
                pv_estimate90 = entry.get("pv_estimate90", pv_estimate)
                pv_estimateCL = entry.get("pv_estimateCL", pv_estimate)

                pv_estimate /= divide_by
                pv_estimate10 /= divide_by
                pv_estimate90 /= divide_by
                pv_estimateCL /= divide_by

                total_day[day] += pv_estimate
                total_day10[day] += pv_estimate10
                total_day90[day] += pv_estimate90
                total_dayCL[day] += pv_estimateCL

                if day == 0 and this_point > now:
                    total_left_today += pv_estimate
                    total_left_today10 += pv_estimate10
                    total_left_today90 += pv_estimate90
                    total_left_todayCL += pv_estimateCL

                next_point = this_point + timedelta(minutes=point_gap)
                if this_point <= now and next_point > now:
                    power_now = pv_estimate * power_scale
                    power_now10 = pv_estimate10 * power_scale
                    power_now90 = pv_estimate90 * power_scale
                    power_nowCL = pv_estimateCL * power_scale

                    # Add this slot into the total left today but scaled for the time since this point
                    if day == 0:
                        left_this_slot_scale = (point_gap - ((now - this_point).total_seconds() / 60)) / point_gap
                        total_left_today += pv_estimate * left_this_slot_scale
                        total_left_today10 += pv_estimate10 * left_this_slot_scale
                        total_left_today90 += pv_estimate90 * left_this_slot_scale
                        total_left_todayCL += pv_estimateCL * left_this_slot_scale

                fentry = {
                    "period_start": entry["period_start"],
                    "pv_estimate": dp2(pv_estimate * power_scale),
                    "pv_estimate10": dp2(pv_estimate10 * power_scale),
                    "pv_estimate90": dp2(pv_estimate90 * power_scale),
                    "pv_estimateCL": dp2(pv_estimateCL * power_scale),
                }
                forecast_day[day].append(fentry)

        days = min(days, 7)
        for day in range(days):
            if day == 0:
                self.log(
                    "PV Forecast for today is {} ({} 10% {} 90% {} calib) kWh and left today is {} ({} 10% {} 90% {} calib) kWh".format(
                        dp2(total_day[day]),
                        dp2(total_day10[day]),
                        dp2(total_day90[day]),
                        dp2(total_dayCL[day]),
                        dp2(total_left_today),
                        dp2(total_left_today10),
                        dp2(total_left_today90),
                        dp2(total_left_todayCL),
                    )
                )
                self.base.dashboard_item(
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
                        "totalCL": dp2(total_dayCL[day]),
                        "remaining": dp2(total_left_today),
                        "remaining10": dp2(total_left_today10),
                        "remaining90": dp2(total_left_today90),
                        "remainingCL": dp2(total_left_todayCL),
                        "detailedForecast": forecast_day[day],
                    },
                )
                self.base.dashboard_item(
                    "sensor." + self.prefix + "_pv_forecast_h0",
                    state=dp2(power_now),
                    attributes={
                        "friendly_name": "PV Forecast Now",
                        "state_class": "measurement",
                        "unit_of_measurement": "kWh",
                        "icon": "mdi:solar-power",
                        "device_class": "power",
                        "now10": dp2(power_now10),
                        "now90": dp2(power_now90),
                        "nowCL": dp2(power_nowCL),
                    },
                )
            else:
                day_name = "tomorrow" if day == 1 else "d{}".format(day)
                day_name_long = day_name if day == 1 else "day {}".format(day)
                self.log("PV Forecast for day {} is {} ({} 10% {} 90% {} CL) kWh".format(day_name, dp2(total_day[day]), dp2(total_day10[day]), dp2(total_day90[day]), dp2(total_dayCL[day])))

                self.base.dashboard_item(
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
                        "totalCL": dp2(total_dayCL[day]),
                        "detailedForecast": forecast_day[day],
                    },
                )

    def pv_calibration(self, pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10, divide_by, max_kwh):
        """
        Perform PV calibration based on historical data and forecast data.
        This will adjust the forecast data based on historical PV production and forecast data.
        It will also create pv_estimate10 and pv_estimate90 data if create_pv10 is True.
        """
        self.log("PV Calibration: Fetching PV data for calibration")

        days = 10
        pv_power_hist, pv_power_hist_days = history_attribute_to_minute_data(
            self.now_utc, prune_today(history_attribute(self.base.get_history_wrapper(self.prefix + ".pv_power", days, required=False)), self.now_utc, self.midnight_utc, prune=False, intermediate=True)
        )
        pv_forecast, pv_forecast_hist_days = history_attribute_to_minute_data(
            self.now_utc, prune_today(history_attribute(self.base.get_history_wrapper("sensor." + self.prefix + "_pv_forecast_h0", days, required=False)), self.now_utc, self.midnight_utc, prune=False, intermediate=True)
        )

        hist_days = min(pv_power_hist_days, pv_forecast_hist_days)
        enabled_calibration = True
        if hist_days < 3:
            enabled_calibration = False
            self.log("PV Calibration: Not enough historical data for calibration, only {} days of history".format(hist_days))

        pv_power_hist_by_slot = {}
        pv_power_hist_by_slot_count = {}
        pv_forecast_by_slot = {}
        pv_forecast_by_slot_count = {}
        past_day_forecast = {}
        past_day_actual = {}

        for minute in pv_power_hist:
            minute_absolute = self.minutes_now - minute
            if minute_absolute < 0:
                days_prev = int(abs(minute_absolute) / (24 * 60)) + 1
                slot_abs = minute_absolute % (24 * 60)
                slot = int(slot_abs / 30) * 30
                pv_power_hist_by_slot[slot] = pv_power_hist_by_slot.get(slot, 0) + pv_power_hist[minute]
                pv_power_hist_by_slot_count[slot] = pv_power_hist_by_slot_count.get(slot, 0) + 1
                past_day_actual[days_prev] = past_day_actual.get(days_prev, 0) + pv_power_hist[minute]

        for slot in pv_power_hist_by_slot:
            if pv_power_hist_by_slot_count[slot] > 0:
                pv_power_hist_by_slot[slot] = dp4(pv_power_hist_by_slot[slot] / pv_power_hist_by_slot_count[slot])

        for minute in pv_forecast:
            minute_absolute = self.minutes_now - minute
            if minute_absolute < 0:
                slot_abs = minute_absolute % (24 * 60)
                slot = int(slot_abs / 30) * 30
                pv_forecast_by_slot[slot] = pv_forecast_by_slot.get(slot, 0) + pv_forecast[minute]
                pv_forecast_by_slot_count[slot] = pv_forecast_by_slot_count.get(slot, 0) + 1

                days_prev = int(abs(minute_absolute) / (24 * 60)) + 1
                past_day_forecast[days_prev] = past_day_forecast.get(days_prev, 0) + pv_forecast[minute]

        worst_day_scaling = 1.0
        best_day_scaling = 1.0
        for day in past_day_forecast:
            past_day_forecast[day] = dp4(past_day_forecast[day] / 60.0)  # Convert to kWh
            past_day_actual[day] = dp4(past_day_actual.get(day, 0) / 60.0)  # Convert to kWh
            scaling_factor = dp4(past_day_actual[day] / past_day_forecast[day] if past_day_forecast[day] > 0 else 1.0)
            worst_day_scaling = min(worst_day_scaling, scaling_factor)
            best_day_scaling = max(best_day_scaling, scaling_factor)
            self.log("PV Calibration: Past day {} had {} kWh of PV forecast data and actual {} kWh".format(day, dp2(past_day_forecast[day]), dp2(past_day_actual[day])))

        # Clamp best and worst day scaling factors to sensible values
        worst_day_scaling = max(worst_day_scaling, 0.5)
        best_day_scaling = min(best_day_scaling, 1.7)
        if not enabled_calibration:
            worst_day_scaling = 0.7
            best_day_scaling = 1.3
        self.log("PV Calibration: Worst day scaling factor {} best day scaling factor {}".format(dp2(worst_day_scaling), dp2(best_day_scaling)))

        for slot in pv_forecast_by_slot:
            if pv_forecast_by_slot_count[slot] > 0:
                pv_forecast_by_slot[slot] = dp4(pv_forecast_by_slot[slot] / pv_forecast_by_slot_count[slot])

        total_production = 0
        for slot in range(0, 24 * 60, 30):
            total_production += pv_power_hist_by_slot.get(slot, 0)

        total_forecast = 0
        for slot in range(0, 24 * 60, 30):
            total_forecast += pv_forecast_by_slot.get(slot, 0)

        pv_distribution = {}
        forecast_distribution = {}
        slot_adjustment = {}
        for slot in range(0, 24 * 60, 30):
            pv_distribution[slot] = dp4((pv_power_hist_by_slot.get(slot, 0)) / total_production if total_production > 0 else 0)
            forecast_distribution[slot] = dp4((pv_forecast_by_slot.get(slot, 0)) / total_forecast if total_forecast > 0 else 0)

            slot_adjustment[slot] = pv_power_hist_by_slot.get(slot, 0) / pv_forecast_by_slot.get(slot, 0) if pv_forecast_by_slot.get(slot, 0) > 0.01 else 1.0
            slot_adjustment[slot] = max(min(slot_adjustment[slot], 2.0), 0.5)  # Clamp adjustment factor to sensible values

            # Override if we don't have enough data
            if not enabled_calibration:
                slot_adjustment[slot] = 1.0

        total_adjustment = dp4(total_production / total_forecast if total_forecast > 0 else 1.0)
        total_adjustment = max(min(total_adjustment, 2.0), 0.5)
        if not enabled_calibration:
            total_adjustment = 1.0
        self.log("PV Calibration: PV production: {} kWh, Total forecast: {} kWh adjustment {}x slot adjustments {} max_kwh {} divide_by {}".format(dp2(total_production), dp2(total_forecast), total_adjustment, slot_adjustment, max_kwh, divide_by))

        # Look at PV forecast today and an adjusted version
        pv_forecast_minute_adjusted = {}
        for minute in range(0, max(pv_forecast_minute.keys()) + 1):
            pv_value = pv_forecast_minute.get(minute, 0)
            slot = (int(minute / 30) * 30) % (24 * 60)
            pv_forecast_minute_adjusted[minute] = pv_value * slot_adjustment.get(slot, 1.0)

        pv_estimateCL = {}
        pv_estimate10 = {}
        pv_estimate90 = {}
        for minute in range(0, max(pv_forecast_minute.keys()) + 1, 30):
            pv_value = 0
            for offset in range(0, 30, 1):
                pv_value += pv_forecast_minute_adjusted.get(minute + offset, 0)
            # Force timezone to UTC
            pv_estimateCL[minute] = min(dp4(pv_value), max_kwh / 2)  # Clamp to max_kwh, divide max by 2 due to 30 minute slots
            pv_estimate10[minute] = min(dp4(pv_value * worst_day_scaling), max_kwh / 2)
            pv_estimate90[minute] = min(dp4(pv_value * best_day_scaling), max_kwh / 2)

        for entry in pv_forecast_data:
            period_start = entry.get("period_start", "")
            if period_start:
                minutes_since_midnight = (datetime.strptime(period_start, TIME_FORMAT) - self.midnight_utc).total_seconds() / 60
                slot = int(minutes_since_midnight / 30) * 30
                calibrated = pv_estimateCL.get(slot, None)
                calibrated10 = pv_estimate10.get(slot, None)
                calibrated90 = pv_estimate90.get(slot, None)

                # When we store the data we have to reverse the divide_by factor
                if calibrated is not None:
                    entry["pv_estimateCL"] = calibrated * divide_by
                if create_pv10 and (calibrated10 is not None):
                    entry["pv_estimate10"] = calibrated10 * divide_by
                if create_pv10 and (calibrated90 is not None):
                    entry["pv_estimate90"] = calibrated90 * divide_by

        # Creation of PV10 data using worst day scaling factor
        if create_pv10:
            for minute in range(0, max(pv_forecast_minute_adjusted.keys()) + 1):
                pv_value = pv_forecast_minute_adjusted.get(minute, 0)
                # Use the worst day scaling factor to create pv_estimate10
                pv_forecast_minute10[minute] = dp4(pv_value * worst_day_scaling)
            self.log("PV Calibration: Created pv_estimate10/pv_estimate90 data using worst day scaling factor {}".format(dp2(worst_day_scaling)))

        # Do we use calibrated or raw data?
        if self.base.get_arg("metric_pv_calibration_enable", default=True):
            self.log("PV Calibration: Using calibrated PV data")
            return pv_forecast_minute_adjusted, pv_forecast_minute10, pv_forecast_data
        else:
            return pv_forecast_minute, pv_forecast_minute10, pv_forecast_data

    def pack_and_store_forecast(self, pv_forecast_minute, pv_forecast_minute10):
        pv_forecast_pack = {}
        pv_forecast_pack10 = {}

        prev_value = -1
        prev_value10 = -1

        for minute in range(0, self.forecast_days * 24 * 60):
            current_value = dp4(pv_forecast_minute.get(minute, 0))
            current_value10 = dp4(pv_forecast_minute10.get(minute, 0))
            if current_value != prev_value:
                pv_forecast_pack[minute] = current_value
                prev_value = current_value
            if current_value10 != prev_value10:
                pv_forecast_pack10[minute] = current_value10
                prev_value10 = current_value10

        current_pv_power = dp4(pv_forecast_minute.get(self.minutes_now, 0))

        self.base.dashboard_item(
            "sensor." + self.prefix + "_pv_forecast_raw",
            state=current_pv_power,
            attributes={"friendly_name": "PV Forecast minute data", "icon": "mdi:solar-power", "forecast": pv_forecast_pack, "forecast10": pv_forecast_pack10, "unit_of_measurement": "kW", "device_class": "power", "state_class": "measurement"},
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
        create_pv10 = False
        max_kwh = 9999

        self.now_utc = datetime.now(timezone.utc)
        self.midnight_utc = self.now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = int((self.now_utc - self.midnight_utc).seconds / 60 / PREDICT_STEP) * PREDICT_STEP

        if self.forecast_solar:
            self.log("Obtaining solar forecast from Forecast Solar API")
            pv_forecast_data, max_kwh = self.download_forecast_solar_data()
            divide_by = 30.0
            create_pv10 = True
        elif self.solcast_host:
            self.log("Obtaining solar forecast from Solcast API")
            pv_forecast_data = self.download_solcast_data()
            divide_by = 30.0
        else:
            self.log("Using Solcast integration from inside HA for solar forecast")

            # Fetch data from each sensor
            for argname in ["pv_forecast_today", "pv_forecast_tomorrow", "pv_forecast_d3", "pv_forecast_d4"]:
                arg_value = getattr(self, argname, None)
                data, total_data, total_sensor = self.fetch_pv_datapoints(argname, arg_value)
                if data:
                    self.log("PV Data for {} total {} kWh".format(argname, total_sensor))
                    pv_forecast_data += data

                    if argname == "pv_forecast_today":
                        pv_forecast_total_data += total_data
                        pv_forecast_total_sensor += total_sensor

            # Work out data scale factor so it adds up (New Solcast is in kW but old was kWH)
            factor = 1.0
            if pv_forecast_total_data > 0.0 and pv_forecast_total_sensor > 0.0:
                factor = round((pv_forecast_total_data / pv_forecast_total_sensor), 1)
            # We want to divide the data into single minute slots
            divide_by = dp2(30 * factor)

            if factor != 1.0 and factor != 2.0:
                self.log("Warn: PV Forecast today adds up to {} kWh but total sensors add up to {} kWh, this is unexpected and hence data maybe misleading (factor {})".format(pv_forecast_total_data, pv_forecast_total_sensor, factor))
            else:
                self.log("PV Forecast today adds up to {} kWh and total sensors add up to {} kWh, factor is {}".format(pv_forecast_total_data, pv_forecast_total_sensor, factor))

        if pv_forecast_data:
            pv_forecast_minute, _ = minute_data(
                pv_forecast_data,
                self.forecast_days,
                self.midnight_utc,
                "pv_estimate",
                "period_start",
                backwards=False,
                divide_by=divide_by,
                scale=self.pv_scaling,
                spreading=30,
            )
            pv_forecast_minute10, _ = minute_data(
                pv_forecast_data,
                self.forecast_days,
                self.midnight_utc,
                "pv_estimate10",
                "period_start",
                backwards=False,
                divide_by=divide_by,
                scale=self.pv_scaling,
                spreading=30,
            )

            # Run calibration on the data
            pv_forecast_minute, pv_forecast_minute10, pv_forecast_data = self.pv_calibration(pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10, divide_by / 30.0, max_kwh)
            self.publish_pv_stats(pv_forecast_data, divide_by / 30.0, 30)
            self.pack_and_store_forecast(pv_forecast_minute, pv_forecast_minute10)
            self.last_success_timestamp = datetime.now(timezone.utc)
        else:
            self.log("Warn: No solar data has been configured.")
