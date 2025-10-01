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
import time
import asyncio
from datetime import datetime, timedelta
from utils import dp1, dp2, dp4
from config import TIME_FORMAT, TIME_FORMAT_SOLCAST, TIME_FORMAT_FORECAST_SOLAR

"""
Solcast class deals with fetching solar predictions, processing the data and publishing the results.
"""


class Solcast:
    def __init__(self, base=None):
        """
        Initialize Solcast component following standard component pattern
        """
        # Accept base parameter for component pattern (like GECloud)
        if base is not None:
            self.base = base
            self.log = base.log
            # Access base object methods and properties through base
            self.get_arg = base.get_arg
            self.args = base.args
            self.get_state_wrapper = base.get_state_wrapper
            self.record_status = base.record_status
        else:
            # Legacy support for direct instantiation
            self.base = None

        # Component state
        self.api_started = False
        self.stop_thread = False

        # Initialize metrics
        self.solcast_requests_total = 0
        self.solcast_failures_total = 0
        self.solcast_last_success_timestamp = None
        self.forecast_solar_requests_total = 0
        self.forecast_solar_failures_total = 0
        self.forecast_solar_last_success_timestamp = None

        # Initialize data storage
        self.solcast_data = {}
        self.forecast_solar_data = {}
        self.solcast_api_limit = None
        self.solcast_api_used = None

    @property
    def midnight_utc(self):
        """Get midnight UTC from base"""
        if hasattr(self, "base"):
            return self.base.midnight_utc
        return getattr(self, "_midnight_utc", None)

    @midnight_utc.setter
    def midnight_utc(self, value):
        """Set midnight UTC for backward compatibility"""
        self._midnight_utc = value

    @property
    def now_utc(self):
        """Get current UTC time from base"""
        if hasattr(self, "base"):
            return self.base.now_utc
        return datetime.now(tz=pytz.utc)

    def wait_api_started(self):
        """
        Return if the API has started
        """
        return True  # Solcast doesn't need async startup

    def is_alive(self):
        """
        Check if the component is alive
        """
        return not self.stop_thread

    async def start(self):
        """
        Start the component and run autonomous data fetching
        """
        self.stop_thread = False
        self.api_started = True

        # Run autonomous data fetching loop
        while not self.stop_thread:
            try:
                self.fetch_and_publish_forecast()
                # Wait before next fetch (every 8 hours by default)
                fetch_interval = self.get_arg("solcast_poll_hours", 8.0) * 3600
                await asyncio.sleep(fetch_interval)
            except Exception as e:
                self.log(f"Error in Solcast fetch loop: {e}")
                # Wait 1 hour on error then retry
                await asyncio.sleep(3600)

    async def stop(self):
        """
        Stop the component
        """
        self.stop_thread = True
        self.api_started = False

    async def select_event(self, entity_id, value):
        """Handle select events (not used by Solcast)"""
        pass

    async def switch_event(self, entity_id, service):
        """Handle switch events (not used by Solcast)"""
        pass

    async def number_event(self, entity_id, value):
        """Handle number events (not used by Solcast)"""
        pass

    def fetch_and_publish_forecast(self):
        """
        Autonomous fetch and publish forecast data as entities
        """
        # Check if component should be active based on configuration
        if not self.should_be_active():
            return

        self.log("Fetching PV forecast data...")

        # Fetch forecast data using existing logic
        pv_forecast_minute, pv_forecast_minute10 = self._fetch_pv_forecast()

        # Publish standardized entities that Fetch can read
        self.publish_forecast_entities(pv_forecast_minute, pv_forecast_minute10)

    def should_be_active(self):
        """
        Determine if Solcast component should be active based on configuration
        Component should only be active if external APIs are configured:
        1. Solcast API key is present
        2. Forecast.solar settings are configured
        HA entity reading is handled by fetch.py, not this component.
        """
        # Active if Solcast API is configured
        if self.get_arg("solcast_host", None) and self.get_arg("solcast_api_key", None):
            return True

        # Active if Forecast.solar is configured
        if self.get_arg("forecast_solar", []):
            return True

        return False

    def publish_forecast_entities(self, pv_forecast_minute, pv_forecast_minute10):
        """
        Publish standardized forecast entities that Fetch can read
        """
        if not hasattr(self.base, "dashboard_item"):
            return

        # Create forecast data structure that matches what Fetch expects
        forecast_today = []
        forecast_tomorrow = []
        forecast_d3 = []
        forecast_d4 = []

        # Convert minute-level data back to 30-minute periods for publishing
        for day in range(4):  # Today, tomorrow, d+2, d+3
            day_forecast = []
            start_minute = day * 24 * 60
            end_minute = (day + 1) * 24 * 60

            for minute in range(start_minute, end_minute, 30):
                # Calculate average for 30-minute period
                pv_estimate = 0
                pv_estimate10 = 0
                for offset in range(30):
                    pv_estimate += pv_forecast_minute.get(minute + offset, 0)
                    pv_estimate10 += pv_forecast_minute10.get(minute + offset, 0)

                if pv_estimate > 0 or pv_estimate10 > 0:  # Only include periods with data
                    period_start = self.midnight_utc + timedelta(minutes=minute)
                    entry = {"period_start": period_start.strftime(TIME_FORMAT), "pv_estimate": pv_estimate, "pv_estimate10": pv_estimate10, "pv_estimate90": pv_estimate}  # Fallback to main estimate
                    day_forecast.append(entry)

            # Assign to appropriate day
            if day == 0:
                forecast_today = day_forecast
            elif day == 1:
                forecast_tomorrow = day_forecast
            elif day == 2:
                forecast_d3 = day_forecast
            elif day == 3:
                forecast_d4 = day_forecast

        # Publish standard entities that match template expectations
        attributes_forecast = {"friendly_name": "PV Forecast Today", "icon": "mdi:solar-power", "unit_of_measurement": "kWh", "device_class": "energy"}

        self.base.dashboard_item("sensor.pv_forecast_today", state=sum(entry["pv_estimate"] for entry in forecast_today), attributes={**attributes_forecast, "forecast": forecast_today}, app="solcast")

        self.base.dashboard_item("sensor.pv_forecast_tomorrow", state=sum(entry["pv_estimate"] for entry in forecast_tomorrow), attributes={**attributes_forecast, "friendly_name": "PV Forecast Tomorrow", "forecast": forecast_tomorrow}, app="solcast")

        self.base.dashboard_item("sensor.pv_forecast_d3", state=sum(entry["pv_estimate"] for entry in forecast_d3), attributes={**attributes_forecast, "friendly_name": "PV Forecast Day 3", "forecast": forecast_d3}, app="solcast")

        self.base.dashboard_item("sensor.pv_forecast_d4", state=sum(entry["pv_estimate"] for entry in forecast_d4), attributes={**attributes_forecast, "friendly_name": "PV Forecast Day 4", "forecast": forecast_d4}, app="solcast")

        self.log(f"Published forecast entities: today={len(forecast_today)} tomorrow={len(forecast_tomorrow)} d3={len(forecast_d3)} d4={len(forecast_d4)} periods")

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
                    self.solcast_last_success_timestamp = time.time()
                if is_forecast_solar_api:
                    self.forecast_solar_last_success_timestamp = time.time()
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
        cache_path = self.config_root + "/cache"
        cache_path_p = self.config_root_p + "/cache"

        self.forecast_solar_data = {}
        cache_file = cache_path + "/forecast_solar.json"
        cache_file_p = cache_path_p + "/forecast_solar.json"

        if os.path.exists(cache_file):
            try:
                with open(cache_file) as f:
                    self.forecast_solar_data = json.load(f)
            except Exception as e:
                self.log("Warn: Error loading forecast.solar cache file {}, error {}".format(cache_file_p, e))
                self.log("Warn: " + traceback.format_exc())
                os.remove(cache_file)

        configs = self.args.get("forecast_solar", [])
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

            data = self.cache_get_url(url, params={}, max_age=self.get_arg("forecast_solar_max_age", 8.0) * 60)
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

                # If point is in the future then accumulate
                if this_point > now:
                    if day == 0:
                        total_left_today += pv_estimate
                        total_left_today10 += pv_estimate10
                        total_left_today90 += pv_estimate90
                        total_left_todayCL += pv_estimateCL

                    # Calc current power, take sample as half way through the window
                    if (this_point - now).total_seconds() < (point_gap * 60 / 2):
                        power_now = pv_estimate * power_scale
                        power_now10 = pv_estimate10 * power_scale
                        power_now90 = pv_estimate90 * power_scale
                        power_nowCL = pv_estimateCL * power_scale

                forecast_day[day].append(entry)
        days = min(days, 7)

        if hasattr(self.base, "set_state_wrapper"):
            # Write sensors
            self.base.set_state_wrapper("sensor.predbat_pv_now", power_now * 1000.0)
            self.base.set_state_wrapper("sensor.predbat_pv_now10", power_now10 * 1000.0)
            self.base.set_state_wrapper("sensor.predbat_pv_now90", power_now90 * 1000.0)
            self.base.set_state_wrapper("sensor.predbat_pv_today", total_day[0])
            self.base.set_state_wrapper("sensor.predbat_pv_today10", total_day10[0])
            self.base.set_state_wrapper("sensor.predbat_pv_today90", total_day90[0])
            self.base.set_state_wrapper("sensor.predbat_pv_tomorrow", total_day[1] if 1 in total_day else 0)
            self.base.set_state_wrapper("sensor.predbat_pv_tomorrow10", total_day10[1] if 1 in total_day10 else 0)
            self.base.set_state_wrapper("sensor.predbat_pv_tomorrow90", total_day90[1] if 1 in total_day90 else 0)

        return total_left_today, total_left_today10, total_left_today90, total_left_todayCL, forecast_day

    def pv_calibration(self, pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10, divide_by, max_kwh):
        """
        Calibrate PV forecast data using scaling or cloud prediction
        """
        pv_forecast_data_calibrated = []

        # Find cloud factor and apply in-day adjustment if cloud prediction is enabled
        if hasattr(self.base, "metric_cloud_enable") and self.base.metric_cloud_enable:
            pv_factor = self.base.get_cloud_factor(self.base.minutes_now, pv_forecast_minute, pv_forecast_minute10)
        else:
            pv_factor = 1.0

        # Calibrate solar data or use direct forecast from Home Assistant
        if pv_forecast_data:
            period = 30  # Period in minutes for each forecast entry
            power_scale = 60 / period  # Scale kwh to power
            scale_today = self.get_arg("pv_scaling", 1.0)
            scale_tomorrow = self.get_arg("pv_scaling_tomorrow", 1.0)
            scale_rest = self.get_arg("pv_scaling_rest", 1.0)

            for entry in pv_forecast_data:
                data = entry.copy()
                try:
                    period_start_str = data.get("period_start", "")
                    period_start = datetime.strptime(period_start_str, self.time_format)
                    data["period_start"] = period_start
                except (ValueError, TypeError):
                    continue

                # Apply scaling based on day
                day = (period_start - self.midnight_utc).days
                if day == 0:
                    scale = scale_today
                elif day == 1:
                    scale = scale_tomorrow
                else:
                    scale = scale_rest

                # Apply cloud factor and scaling
                if pv_factor and period_start > self.now_utc:
                    data["pv_estimateCL"] = data["pv_estimate"] * (1.0 - pv_factor)
                    data["pv_estimate10"] = data.get("pv_estimate10", data["pv_estimate"])
                    data["pv_estimate90"] = data.get("pv_estimate90", data["pv_estimate"])
                else:
                    data["pv_estimateCL"] = data["pv_estimate"]

                data["pv_estimate"] *= scale / divide_by
                data["pv_estimate10"] = data.get("pv_estimate10", data["pv_estimate"]) * scale / divide_by
                data["pv_estimate90"] = data.get("pv_estimate90", data["pv_estimate"]) * scale / divide_by
                data["pv_estimateCL"] *= scale / divide_by

                pv_forecast_data_calibrated.append(data)

        return pv_forecast_data_calibrated

    def _fetch_pv_forecast(self):
        """
        Fetch the PV Forecast data from external APIs only
        Component only handles Solcast and Forecast.solar APIs
        HA entity reading is handled by fetch.py
        """
        pv_forecast_minute = {}
        pv_forecast_minute10 = {}

        # Solcast cloud API call
        pv_forecast_data = None
        divide_by = 1.0
        if self.get_arg("solcast_host", None):
            pv_forecast_data = self.download_solcast_data()
            if not pv_forecast_data:
                self.log("Warn: No solar data from Solcast cloud, using default of 0")
        # Forecast Solar cloud API call
        elif self.get_arg("forecast_solar", []):
            pv_forecast_data, max_kwh = self.download_forecast_solar_data()
            if not pv_forecast_data:
                self.log("Warn: No solar data from Forecast.Solar cloud, using default of 0")
        else:
            # Component should not reach here if properly configured
            self.log("Warn: Solcast component active but no API configuration found")
            return pv_forecast_minute, pv_forecast_minute10

        # Create 10% forecast if not present
        create_pv10 = False
        if pv_forecast_data:
            for entry in pv_forecast_data:
                if "pv_estimate10" not in entry:
                    create_pv10 = True
                    break

        if create_pv10:
            for entry in pv_forecast_data:
                entry["pv_estimate10"] = entry.get("pv_estimate", 0) * 0.1
                entry["pv_estimate90"] = entry.get("pv_estimate", 0)

        # Calibrate PV data
        if pv_forecast_data:
            pv_forecast_data = self.pv_calibration(pv_forecast_minute, pv_forecast_minute10, pv_forecast_data, create_pv10, divide_by, 0)

        # Publish PV stats
        if pv_forecast_data:
            self.publish_pv_stats(pv_forecast_data, 1.0, 30)

        # Convert forecast data to minute-level data
        if pv_forecast_data:
            for entry in pv_forecast_data:
                period_start = entry.get("period_start")
                if isinstance(period_start, str):
                    try:
                        period_start = datetime.strptime(period_start, TIME_FORMAT)
                    except (ValueError, TypeError):
                        continue

                minutes = int((period_start - self.midnight_utc).total_seconds() / 60)
                # Spread 30-minute forecast over each minute
                for minute_offset in range(30):
                    minute = minutes + minute_offset
                    pv_forecast_minute[minute] = pv_forecast_minute.get(minute, 0) + entry.get("pv_estimate", 0) / 30
                    pv_forecast_minute10[minute] = pv_forecast_minute10.get(minute, 0) + entry.get("pv_estimate10", 0) / 30

        return pv_forecast_minute, pv_forecast_minute10
