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

from utils import str2time
import time
import asyncio
import traceback
from datetime import datetime, timezone, timedelta


class CarbonAPI:
    """Carbon intensity client."""

    def __init__(self, postcode, automatic, base):
        self.base = base
        self.log = base.log
        self.postcode = postcode
        self.prefix = base.prefix
        self.api_started = False
        self.automatic = automatic
        self.stop_api = False
        self.failures_total = 0
        self.last_success_timestamp = None
        self.carbon_data_points = []

    def wait_api_started(self):
        """
        Wait for the API to start
        """
        self.log("Carbon API: Waiting for API to start")
        count = 0
        while not self.api_started and count < 240:
            time.sleep(1)
            count += 1
        if not self.api_started:
            self.log("Warn: Carbon API: Failed to start")
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

    async def fetch_carbon_data(self):
        """
        Fetch the latest carbon data, update only if data is at least 4 hours old
        """
        last_updated = self.last_success_timestamp
        if last_updated is not None and datetime.now(timezone.utc) - last_updated < timedelta(hours=4):
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
            async with self.base.session.get(url, timeout=30) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                        data_points = data.get("data", {}).get("data", [])
                        if data_points:
                            self.last_success_timestamp = datetime.now(timezone.utc)
                            for point in data_points:
                                from_time = point.get("from", None)
                                to_time = point.get("to", None)
                                intensity = point.get("intensity", {}).get("forecast", None)
                                if from_time and to_time and intensity is not None:
                                    collected_data.append({"from": from_time, "to": to_time, "intensity": intensity})
                        else:
                            self.failures_total += 1
                            self.log("Warn: Carbon API: No data points found in response for date {}".format(date))
                    except Exception as e:
                        self.log(f"Warn: Carbon API: Failed to parse JSON response: {e}")
                else:
                    self.failures_total += 1
                    self.log(f"Warn: Carbon API: Failed to fetch data, status code {response.status}")
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
            from_time = str2time(point["from"])
            to_time = str2time(point["to"])
            if now_utc >= from_time and now_utc < to_time:
                value_now = point["intensity"]

        self.base.dashboard_item("sensor." + self.prefix + "_carbon_intensity", state=value_now, app="carbon", attributes={"unit_of_measurement": "gCO2/kWh", "friendly_name": "Carbon Intensity", "forecast": self.carbon_data_points})

    async def automatic_config(self):
        """
        Automatic configuration based on carbon data
        """
        self.base.args["carbon_intensity"] = "sensor." + self.prefix + "_carbon_intensity"

    async def start(self):
        """
        Main run loop
        """

        count_seconds = 0
        self.api_started = True
        while not self.stop_api:
            try:
                if count_seconds % (15 * 60) == 0:  # Every 15 minutes
                    await self.fetch_carbon_data()

                if count_seconds == 0 and self.automatic:
                    await self.automatic_config()

            except Exception as e:
                self.log("Error: Carbon API: {}".format(e))
                self.log("Error: " + traceback.format_exc())

            await asyncio.sleep(15)
            count_seconds += 15

        print("Carbon API: Stopped")

    async def stop(self):
        self.stop_api = True
