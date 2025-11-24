# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# UK Carbon Intensity library
# -----------------------------------------------------------------------------

# https://api.carbonintensity.org.uk/regional/intensity/{date}}/fw48h/postcode/{postcode}

"""
{"data":{"regionid":11,"dnoregion":"WPD South West","shortname":"South West England","postcode":"BS16","data":[{"from":"2025-10-22T23:30Z","to":"2025-10-23T00:00Z","intensity":{"forecast":162,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.4},{"fuel":"coal","perc":0},{"fuel":"imports","perc":44.2},{"fuel":"gas","perc":32.9},{"fuel":"nuclear","perc":1},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":21.3}]},{"from":"2025-10-23T00:00Z","to":"2025-10-23T00:30Z","intensity":{"forecast":164,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.2},{"fuel":"coal","perc":0},{"fuel":"imports","perc":44.1},{"fuel":"gas","perc":33.5},{"fuel":"nuclear","perc":0.9},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":21.2}]},{"from":"2025-10-23T00:30Z","to":"2025-10-23T01:00Z","intensity":{"forecast":165,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0},{"fuel":"coal","perc":0},{"fuel":"imports","perc":43.5},{"fuel":"gas","perc":33.8},{"fuel":"nuclear","perc":0.8},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":21.7}]},{"from":"2025-10-23T01:00Z","to":"2025-10-23T01:30Z","intensity":{"forecast":161,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.1},{"fuel":"coal","perc":0},{"fuel":"imports","perc":42.5},{"fuel":"gas","perc":32.9},{"fuel":"nuclear","perc":1},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":23.5}]},{"from":"2025-10-23T01:30Z","to":"2025-10-23T02:00Z","intensity":{"forecast":157,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.1},{"fuel":"coal","perc":0},{"fuel":"imports","perc":40.8},{"fuel":"gas","perc":32.2},{"fuel":"nuclear","perc":1.1},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":25.7}]},{"from":"2025-10-23T02:00Z","to":"2025-10-23T02:30Z","intensity":{"forecast":143,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":1},{"fuel":"coal","perc":0},{"fuel":"imports","perc":35.6},{"fuel":"gas","perc":30.3},{"fuel":"nuclear","perc":1.2},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":31.8}]},{"from":"2025-10-23T02:30Z","to":"2025-10-23T03:00Z","intensity":{"forecast":134,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.3},{"fuel":"coal","perc":0},{"fuel":"imports","perc":39.4},{"fuel":"gas","perc":26.9},{"fuel":"nuclear","perc":1.1},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":32.2}]},{"from":"2025-10-23T03:00Z","to":"2025-10-23T03:30Z","intensity":{"forecast":137,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.3},{"fuel":"coal","perc":0},{"fuel":"imports","perc":39.3},{"fuel":"gas","perc":27.6},{"fuel":"nuclear","perc":1.1},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":31.7}]},{"from":"2025-10-23T03:30Z","to":"2025-10-23T04:00Z","intensity":{"forecast":143,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.5},{"fuel":"coal","perc":0},{"fuel":"imports","perc":37.8},{"fuel":"gas","perc":29.2},{"fuel":"nuclear","perc":1.1},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":31.4}]},{"from":"2025-10-23T04:00Z","to":"2025-10-23T04:30Z","intensity":{"forecast":137,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.5},{"fuel":"coal","perc":0},{"fuel":"imports","perc":36.9},{"fuel":"gas","perc":28},{"fuel":"nuclear","perc":1},{"fuel":"other","perc":0},
"""

TIME_FORMAT_CARBON = "%Y-%m-%dT%H:%MZ"

import asyncio
import traceback
from datetime import datetime, timezone, timedelta
import requests
from config import TIME_FORMAT_HA
from component_base import ComponentBase


class CarbonAPI(ComponentBase):
    """Carbon intensity client."""

    def initialize(self, postcode, automatic):
        """Initialize the CarbonAPI component"""
        self.postcode = postcode
        self.automatic = automatic
        self.failures_total = 0
        self.last_updated_timestamp = None
        self.carbon_data_points = []

    async def fetch_carbon_data(self):
        """
        Fetch the latest carbon data, update only if data is at least 4 hours old
        """
        last_updated = self.last_updated_timestamp
        if last_updated is not None and datetime.now(timezone.utc) - last_updated < timedelta(hours=4):
            self.update_success_timestamp()
            return

        self.log("Carbon API: Fetching latest carbon data for postcode {}".format(self.postcode))

        date_now = datetime.now().strftime("%Y-%m-%d")
        date_plus_48 = (datetime.now() + timedelta(hours=48)).strftime("%Y-%m-%d")
        postcode = self.postcode

        # Shorten postcode, remove anything after the space as we just need the stem
        if " " in postcode:
            postcode = postcode.split(" ")[0]

        collected_data = []

        for date in [date_now, date_plus_48]:
            url = f"https://api.carbonintensity.org.uk/regional/intensity/{date}/fw48h/postcode/{postcode}"
            try:
                response = requests.get(url, timeout=30)
                if response.status_code == 200:
                    try:
                        data = response.json()
                        data_points = data.get("data", {}).get("data", [])
                        if data_points:
                            self.update_success_timestamp()
                            self.last_updated_timestamp = self.last_updated_time()
                            for point in data_points:
                                from_time = point.get("from", None)
                                to_time = point.get("to", None)
                                intensity = point.get("intensity", {}).get("forecast", None)
                                try:
                                    # Use TIME_FORMAT_CARBON to parse time strings
                                    from_time = datetime.strptime(from_time, TIME_FORMAT_CARBON).replace(tzinfo=timezone.utc)
                                    to_time = datetime.strptime(to_time, TIME_FORMAT_CARBON).replace(tzinfo=timezone.utc)
                                except Exception as e:
                                    from_time = None
                                    to_time = None
                                if from_time and to_time and intensity is not None:
                                    # Store using string of TIME_FORMAT_HA
                                    collected_data.append({"from": from_time.strftime(TIME_FORMAT_HA), "to": to_time.strftime(TIME_FORMAT_HA), "intensity": intensity})
                        else:
                            self.failures_total += 1
                            self.log("Warn: Carbon API: No data points found in response for date {}".format(date))
                    except Exception as e:
                        self.log(f"Warn: Carbon API: Failed to parse JSON response: {e}")
                else:
                    self.failures_total += 1
                    self.log(f"Warn: Carbon API: Failed to fetch data, status code {response.status_code}")
            except requests.RequestException as e:
                self.failures_total += 1
                self.log(f"Warn: Carbon API: Request failed: {e}")
        if collected_data:
            self.carbon_data_points = collected_data
            self.publish_carbon_data()
            self.log("Carbon API: Successfully fetched {} data points".format(len(collected_data)))

    def publish_carbon_data(self):
        """
        Publish the latest carbon data to the system
        """
        value_now = "unknown"
        now_utc = datetime.now(timezone.utc)
        for point in self.carbon_data_points:
            from_time = datetime.strptime(point["from"], TIME_FORMAT_HA)
            to_time = datetime.strptime(point["to"], TIME_FORMAT_HA)
            if now_utc >= from_time and now_utc < to_time:
                value_now = point["intensity"]

        self.dashboard_item("sensor." + self.prefix + "_carbon_intensity", state=value_now, app="carbon", attributes={"unit_of_measurement": "gCO2/kWh", "friendly_name": "Carbon Intensity", "forecast": self.carbon_data_points})

    async def automatic_config(self):
        """
        Automatic configuration based on carbon data
        """
        self.set_arg("carbon_intensity", "sensor." + self.prefix + "_carbon_intensity")

    async def run(self, seconds, first):
        """
        Main run loop
        """
        if seconds % (15 * 60) == 0:  # Every 15 minutes
            await self.fetch_carbon_data()

        if first and self.automatic:
            await self.automatic_config()

        return True
