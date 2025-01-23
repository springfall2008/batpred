import requests
import re
from datetime import datetime, timedelta
from config import TIME_FORMAT, TIME_FORMAT_OCTOPUS
from utils import str2time, minutes_to_time, dp1, dp2


class Energidataservice:
    def fetch_energidataservice_rates(self, entity_id, adjust_key=None):
        """
        Fetch the Energi Data Service rates from the sensor, add tariffs from attributes,
        and convert to 30-minute intervals.

        :param entity_id: The entity_id of the sensor
        :param adjust_key: The key used to find intelligently adjusted rates
        :return: Processed rate data in 30-minute intervals
        """
        data_all = []
        rate_data = {}

        if entity_id:
            if self.debug_enable:
                self.log("Fetch Energi Data Service rates from {}".format(entity_id))

            # Fetch raw rates for today
            data_import_today = self.get_state_wrapper(entity_id=entity_id, attribute="raw_today")
            if data_import_today:
                data_all += data_import_today
            else:
                self.log(f"Warn: No Energi Data Service data in sensor {entity_id} attribute 'raw_today'")

            # Fetch raw rates for tomorrow
            data_import_tomorrow = self.get_state_wrapper(entity_id=entity_id, attribute="raw_tomorrow")
            if data_import_tomorrow:
                data_all += data_import_tomorrow
            else:
                self.log(f"Warn: No Energi Data Service data in sensor {entity_id} attribute 'raw_tomorrow'")

            # Fetch tariffs from the sensor attributes
            tariffs = self.get_state_wrapper(entity_id=entity_id, attribute="tariffs")
            if not tariffs:
                self.log(f"Warn: No tariffs found in sensor {entity_id} attribute 'tariffs'")
                tariffs = {}

        if data_all:
            # Add the tariffs to each rate based on the hour
            for entry in data_all:
                hour = str(entry.get("hour"))  # Convert hour to string to match tariff keys
                tariff = tariffs.get(hour, 0)  # Default to 0 if no tariff is found for the hour
                entry["price_with_tariff"] = entry.get("price", 0) + tariff

            # Convert hourly data to 30-minute intervals
            rate_data = self.hourly_to_half_hourly(data_all, self.forecast_days + 1, self.midnight_utc, rate_key="price_with_tariff", from_key="hour", adjust_key=adjust_key, scale=1.0)

        return rate_data

    def hourly_to_half_hourly(self, data, forecast_days, midnight_utc, rate_key, from_key, adjust_key=None, scale=1.0):
        """
        Convert hourly rate data into 30-minute rate data.

        :param data: List of rate dictionaries.
        :param forecast_days: Number of days to forecast.
        :param midnight_utc: Reference datetime object for midnight UTC.
        :param rate_key: Key to extract rate value.
        :param from_key: Key to extract start time.
        :param adjust_key: Key for intelligent adjustments.
        :param scale: Scaling factor for rate values.
        :return: Dictionary with 30-minute rates.
        """
        rate_data = {}
        for entry in data:
            start_time_str = entry.get(from_key)
            rate = entry.get(rate_key, 0) / scale  # Apply scaling

            # Parse the start time
            try:
                start_time = datetime.fromisoformat(start_time_str)
            except ValueError:
                self.log(f"Warn: Invalid time format '{start_time_str}' in data")
                continue

            # Determine the two 30-minute slots within the hour
            slot1_start = start_time
            slot1_end = start_time + timedelta(minutes=30)
            slot2_start = slot1_end
            slot2_end = start_time + timedelta(hours=1)

            # Calculate minutes since midnight for each slot
            slot1_start_min = int((slot1_start - midnight_utc).total_seconds() / 60)
            slot1_end_min = int((slot1_end - midnight_utc).total_seconds() / 60)
            slot2_start_min = slot1_end_min
            slot2_end_min = int((slot2_end - midnight_utc).total_seconds() / 60)

            # Assign the rate to each 30-minute slot
            for slot_start_min, slot_end_min in [(slot1_start_min, slot1_end_min), (slot2_start_min, slot2_end_min)]:
                # Ensure minutes are within forecast range
                if slot_start_min < 0:
                    slot_start_min = 0
                if slot_end_min > self.forecast_minutes:
                    slot_end_min = self.forecast_minutes

                # Assign the rate to each minute within the 30-minute slot
                for minute in range(slot_start_min, slot_end_min):
                    if 0 <= minute < self.forecast_minutes:
                        rate_data[minute] = rate

        # Apply intelligent adjustments if needed
        if adjust_key:
            # Implement any adjustments based on adjust_key
            pass

        return rate_data
