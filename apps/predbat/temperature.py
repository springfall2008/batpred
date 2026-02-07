# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import aiohttp
import asyncio
from datetime import datetime
from utils import dp1
from component_base import ComponentBase


class TemperatureAPI(ComponentBase):
    def initialize(self, temperature_enable, temperature_latitude, temperature_longitude, temperature_url):
        """Initialize the Temperature API component"""
        self.temperature_enable = temperature_enable
        self.temperature_latitude = temperature_latitude
        self.temperature_longitude = temperature_longitude
        self.temperature_url = temperature_url
        self.temperature_cache = {}
        self.temperature_data = None
        self.last_updated_timestamp = None
        self.failures_total = 0

    async def select_event(self, entity_id, value):
        pass

    async def number_event(self, entity_id, value):
        pass

    async def switch_event(self, entity_id, service):
        pass

    async def run(self, seconds, first):
        """
        Main run loop - polls API every hour
        """
        try:
            if not self.temperature_enable:
                return True
            if first or (seconds % (60 * 60) == 0):
                # Fetch temperature data every hour
                temperature_data = await self.fetch_temperature_data()
                if temperature_data is not None:
                    self.temperature_data = temperature_data
                    self.last_updated_timestamp = datetime.now()
                    self.publish_temperature_sensor()
            if self.temperature_data is not None:
                self.update_success_timestamp()
                self.publish_temperature_sensor()
        except Exception as e:
            self.log("Warn: TemperatureAPI: Exception in run loop: {}".format(e))
            # Still return True to keep component alive
            if self.temperature_data is not None:
                # Keep publishing old data even on error
                self.publish_temperature_sensor()

        return True

    def get_coordinates(self):
        """
        Get latitude and longitude, with fallback to zone.home
        """
        # Try config values first
        latitude = self.temperature_latitude
        longitude = self.temperature_longitude

        # If latitude and longitude are not provided, use zone.home
        if latitude is None:
            latitude = self.get_state_wrapper("zone.home", attribute="latitude")
        if longitude is None:
            longitude = self.get_state_wrapper("zone.home", attribute="longitude")

        if latitude is not None and longitude is not None:
            self.log("TemperatureAPI: Using coordinates latitude {}, longitude {}".format(dp1(latitude), dp1(longitude)))
            return latitude, longitude
        else:
            self.log("Warn: TemperatureAPI: No latitude or longitude found, cannot fetch temperature data")
            return None, None

    def build_api_url(self, latitude, longitude):
        """
        Build the API URL with latitude and longitude placeholders replaced
        """
        url = self.temperature_url.replace("LATITUDE", str(latitude)).replace("LONGITUDE", str(longitude))
        return url

    def convert_timezone_offset(self, utc_offset_seconds):
        """
        Convert UTC offset in seconds to ±HH:MM format
        Handles negative offsets correctly
        """
        if utc_offset_seconds >= 0:
            sign = "+"
        else:
            sign = "-"
            utc_offset_seconds = abs(utc_offset_seconds)

        offset_hours = utc_offset_seconds // 3600
        offset_minutes = (utc_offset_seconds % 3600) // 60

        return "{}{:02d}:{:02d}".format(sign, offset_hours, offset_minutes)

    async def fetch_temperature_data(self):
        """
        Fetch temperature data from Open-Meteo API with retry logic
        """
        latitude, longitude = self.get_coordinates()
        if latitude is None or longitude is None:
            return None

        url = self.build_api_url(latitude, longitude)

        # Try up to 3 times with exponential backoff
        max_retries = 3
        for attempt in range(max_retries):
            try:
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            data = await response.json()
                            self.log("TemperatureAPI: Successfully fetched temperature data from Open-Meteo API")
                            self.update_success_timestamp()
                            return data
                        else:
                            self.log("Warn: TemperatureAPI: Failed to fetch data, status code {}".format(response.status))
                            if attempt < max_retries - 1:
                                sleep_time = 2 ** attempt
                                self.log("Warn: TemperatureAPI: Retrying in {} seconds...".format(sleep_time))
                                await asyncio.sleep(sleep_time)
                            else:
                                self.failures_total += 1
                                return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt < max_retries - 1:
                    sleep_time = 2 ** attempt
                    self.log("Warn: TemperatureAPI: Request attempt {} failed: {}. Retrying in {}s...".format(attempt + 1, e, sleep_time))
                    await asyncio.sleep(sleep_time)
                else:
                    self.log("Warn: TemperatureAPI: Request failed after {} attempts: {}".format(max_retries, e))
                    self.failures_total += 1
                    return None
            except Exception as e:
                self.log("Warn: TemperatureAPI: Unexpected error fetching temperature data: {}".format(e))
                self.failures_total += 1
                return None

        return None

    def publish_temperature_sensor(self):
        """
        Publish temperature sensor to Home Assistant
        """
        if self.temperature_data is None:
            return

        try:
            # Extract current temperature
            current = self.temperature_data.get("current", {})
            current_temp = current.get("temperature_2m")

            if current_temp is None:
                self.log("Warn: TemperatureAPI: No current temperature in API response")
                return

            # Get timezone offset
            utc_offset_seconds = self.temperature_data.get("utc_offset_seconds", 0)
            timezone_offset = self.convert_timezone_offset(utc_offset_seconds)

            # Build hourly forecast dictionary
            hourly = self.temperature_data.get("hourly", {})
            hourly_times = hourly.get("time", [])
            hourly_temps = hourly.get("temperature_2m", [])

            forecast = {}
            if len(hourly_times) == len(hourly_temps):
                for time_str, temp in zip(hourly_times, hourly_temps):
                    # Convert ISO8601 time to HA format with timezone
                    # Open-Meteo returns: "2026-02-07T00:00"
                    # HA format: "2026-02-07T00:00:00+00:00"
                    ha_timestamp = "{}:00{}".format(time_str, timezone_offset)
                    forecast[ha_timestamp] = temp

            # Build last_updated string
            last_updated_str = str(self.last_updated_timestamp) if self.last_updated_timestamp else "Never"

            # Publish sensor
            self.dashboard_item(
                "sensor." + self.prefix + "_temperature",
                state=current_temp,
                attributes={
                    "friendly_name": "External Temperature Forecast",
                    "icon": "mdi:thermometer",
                    "unit_of_measurement": "°C",
                    "last_updated": last_updated_str,
                    "results": forecast,
                    "timezone_offset": timezone_offset,
                    "data_points": len(forecast)
                },
                app="temperature"
            )

        except Exception as e:
            self.log("Warn: TemperatureAPI: Error publishing sensor: {}".format(e))
