# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# UK Carbon Intensity library
# -----------------------------------------------------------------------------

# https://api.carbonintensity.org.uk/regional/intensity/{date}}/fw48h/postcode/{postcode}

"""
{"data":{"regionid":11,"dnoregion":"WPD South West","shortname":"South West England","postcode":"BS16","data":[{"from":"2025-10-22T23:30Z","to":"2025-10-23T00:00Z","intensity":{"forecast":162,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.4},{"fuel":"coal","perc":0},{"fuel":"imports","perc":44.2},{"fuel":"gas","perc":32.9},{"fuel":"nuclear","perc":1},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":21.3}]},{"from":"2025-10-23T00:00Z","to":"2025-10-23T00:30Z","intensity":{"forecast":164,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.2},{"fuel":"coal","perc":0},{"fuel":"imports","perc":44.1},{"fuel":"gas","perc":33.5},{"fuel":"nuclear","perc":0.9},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":21.2}]},{"from":"2025-10-23T00:30Z","to":"2025-10-23T01:00Z","intensity":{"forecast":165,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0},{"fuel":"coal","perc":0},{"fuel":"imports","perc":43.5},{"fuel":"gas","perc":33.8},{"fuel":"nuclear","perc":0.8},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":21.7}]},{"from":"2025-10-23T01:00Z","to":"2025-10-23T01:30Z","intensity":{"forecast":161,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.1},{"fuel":"coal","perc":0},{"fuel":"imports","perc":42.5},{"fuel":"gas","perc":32.9},{"fuel":"nuclear","perc":1},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":23.5}]},{"from":"2025-10-23T01:30Z","to":"2025-10-23T02:00Z","intensity":{"forecast":157,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.1},{"fuel":"coal","perc":0},{"fuel":"imports","perc":40.8},{"fuel":"gas","perc":32.2},{"fuel":"nuclear","perc":1.1},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":25.7}]},{"from":"2025-10-23T02:00Z","to":"2025-10-23T02:30Z","intensity":{"forecast":143,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":1},{"fuel":"coal","perc":0},{"fuel":"imports","perc":35.6},{"fuel":"gas","perc":30.3},{"fuel":"nuclear","perc":1.2},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":31.8}]},{"from":"2025-10-23T02:30Z","to":"2025-10-23T03:00Z","intensity":{"forecast":134,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.3},{"fuel":"coal","perc":0},{"fuel":"imports","perc":39.4},{"fuel":"gas","perc":26.9},{"fuel":"nuclear","perc":1.1},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":32.2}]},{"from":"2025-10-23T03:00Z","to":"2025-10-23T03:30Z","intensity":{"forecast":137,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.3},{"fuel":"coal","perc":0},{"fuel":"imports","perc":39.3},{"fuel":"gas","perc":27.6},{"fuel":"nuclear","perc":1.1},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":31.7}]},{"from":"2025-10-23T03:30Z","to":"2025-10-23T04:00Z","intensity":{"forecast":143,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.5},{"fuel":"coal","perc":0},{"fuel":"imports","perc":37.8},{"fuel":"gas","perc":29.2},{"fuel":"nuclear","perc":1.1},{"fuel":"other","perc":0},{"fuel":"hydro","perc":0.1},{"fuel":"solar","perc":0},{"fuel":"wind","perc":31.4}]},{"from":"2025-10-23T04:00Z","to":"2025-10-23T04:30Z","intensity":{"forecast":137,"index":"moderate"},"generationmix":[{"fuel":"biomass","perc":0.5},{"fuel":"coal","perc":0},{"fuel":"imports","perc":36.9},{"fuel":"gas","perc":28},{"fuel":"nuclear","perc":1},{"fuel":"other","perc":0},
"""

TIME_FORMAT_CARBON = "%Y-%m-%dT%H:%MZ"

from datetime import datetime, timezone, timedelta
import asyncio
import sys
import aiohttp
from const import TIME_FORMAT_HA
from component_base import ComponentBase
from mock_base import MockBase as SharedMockBase
from predbat_metrics import record_api_call


class CarbonAPI(ComponentBase):
    """Carbon intensity client."""

    def initialize(self, postcode, automatic):
        """Initialise the CarbonAPI component"""
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

        # The API takes an ISO8601 YYYY-MM-DDThh:mmZ datetime and returns the 48 hours following it,
        # which is the entire forecast horizon it publishes, so one request from now covers everything
        # available. It has to be built in UTC - a local date is a day out between midnight and 01:00
        # under BST. Asking from a date further ahead than the horizon just returns an empty body.
        date_from = datetime.now(timezone.utc).strftime(TIME_FORMAT_CARBON)
        postcode = self.postcode

        # Shorten postcode, remove anything after the space as we just need the stem
        if " " in postcode:
            postcode = postcode.split(" ")[0]

        collected_data = []

        url = f"https://api.carbonintensity.org.uk/regional/intensity/{date_from}/fw48h/postcode/{postcode}"
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        try:
                            data = await response.json()
                            if data:
                                data_points = data.get("data", {}).get("data", [])
                                if data_points:
                                    record_api_call("carbon")
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
                                        except Exception:
                                            from_time = None
                                            to_time = None
                                        if from_time and to_time and intensity is not None:
                                            # Store using string of TIME_FORMAT_HA
                                            collected_data.append({"from": from_time.strftime(TIME_FORMAT_HA), "to": to_time.strftime(TIME_FORMAT_HA), "intensity": intensity})
                                else:
                                    self.failures_total += 1
                                    self.log("Warn: Carbon API: No data points found in response for postcode {} and date {}".format(postcode, date_from))
                            else:  # Carbon API returns 200 but no data if the postcode or date can't be found
                                self.failures_total += 1
                                self.log("Error: Carbon API: No carbon data returned for postcode {} and date {}".format(postcode, date_from))
                        except Exception as e:
                            self.log(f"Warn: Carbon API: Failed to parse JSON response: {e}")
                            record_api_call("carbon", False, "decode_error")
                    else:
                        self.failures_total += 1
                        self.log(f"Warn: Carbon API: Failed to fetch data, API status code {response.status}")
                        record_api_call("carbon", False, "server_error")
        except (aiohttp.ClientError, Exception) as e:
            self.failures_total += 1
            self.log(f"Warn: Carbon API: Request failed: {e}")
            record_api_call("carbon", False, "connection_error")
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
        if first or (seconds % (15 * 60) == 0):  # Every 15 minutes
            await self.fetch_carbon_data()

        if first and self.automatic:
            await self.automatic_config()

        return True


class MockBase(SharedMockBase):  # pragma: no cover
    """Mock base for the Carbon command-line harness, with its own cache root."""

    def __init__(self):
        """Initialise the shared mock with the Carbon cache root."""
        super().__init__(config_root="./temp_carbon")

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Publish an entity, summarising the forecast rather than dumping every half hour slot."""
        if attributes and "forecast" in attributes:
            attributes = dict(attributes)
            attributes["forecast"] = "... {} points".format(len(attributes["forecast"]))
        super().dashboard_item(entity_id, state=state, attributes=attributes, app=app)


async def test_carbon_api(postcode, show_slots=False):  # pragma: no cover
    """
    Test the Carbon Intensity API against a real postcode and run one fetch cycle.

    Reports how far ahead the forecast actually reaches, which is the thing most likely to be
    wrong - the API publishes at most 48 hours and often far less when the upstream forecast is
    late, and the shortfall is otherwise invisible until the plan silently scores the uncovered
    minutes as zero carbon.
    """
    print(f"Testing Carbon Intensity API for postcode {postcode}")

    mock_base = MockBase()

    carbon_api = CarbonAPI(mock_base, postcode=postcode, automatic=True)
    await carbon_api.run(0, True)

    points = carbon_api.carbon_data_points
    print("\nCollected {} data point(s), failures {}".format(len(points), carbon_api.failures_total))

    if not points:
        print("ERROR: No carbon data returned - check the postcode is a real UK one, and that the API is up")
        await carbon_api.final()
        return 1

    now_utc = datetime.now(timezone.utc)
    first_from = datetime.strptime(points[0]["from"], TIME_FORMAT_HA)
    last_to = datetime.strptime(points[-1]["to"], TIME_FORMAT_HA)
    hours_ahead = (last_to - now_utc).total_seconds() / 3600.0

    print("Covers {} -> {}".format(first_from.strftime("%Y-%m-%d %H:%M %Z"), last_to.strftime("%Y-%m-%d %H:%M %Z")))
    print("Forward coverage from now: {:.1f} hours".format(hours_ahead))

    intensities = [point["intensity"] for point in points]
    print("Intensity gCO2/kWh: min {}, max {}, average {:.0f}".format(min(intensities), max(intensities), sum(intensities) / len(intensities)))
    print("Published sensor state: {}".format(mock_base.get_state_wrapper("sensor.predbat_carbon_intensity")))

    if hours_ahead < 48:
        print("\nWarn: the API is publishing only {:.1f} hours ahead, short of the 48 hours it can supply.".format(hours_ahead))
        print("      Predbat fills the rest with carbon_replicate() (fetch.py), repeating the previous day.")

    if show_slots:
        print("\nForecast slots:")
        for point in points:
            slot_from = datetime.strptime(point["from"], TIME_FORMAT_HA)
            marker = " <- now" if slot_from <= now_utc < datetime.strptime(point["to"], TIME_FORMAT_HA) else ""
            print("  {}  {:>4} gCO2/kWh{}".format(slot_from.strftime("%Y-%m-%d %H:%M"), point["intensity"], marker))

    await carbon_api.final()
    print("\nTest completed")
    return 0


def main():  # pragma: no cover
    """
    Main function for command line execution to test the Carbon Intensity API.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Test Carbon Intensity API")
    parser.add_argument("--postcode", required=True, help="Outward postcode to fetch carbon intensity for (e.g. BS16)")
    parser.add_argument("--slots", action="store_true", help="Print every half hour forecast slot as well as the summary")

    args = parser.parse_args()

    return asyncio.run(test_carbon_api(args.postcode, show_slots=args.slots))


if __name__ == "__main__":
    sys.exit(main())
