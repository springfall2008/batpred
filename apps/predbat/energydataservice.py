"""Danish Energi Data Service integration for electricity rates.

Reads hourly electricity rates from the Energi Data Service Home Assistant
integration sensor, applies tariff adjustments, and converts to per-minute
rate dictionaries.
"""

from datetime import datetime, timezone
from utils import dp4
from rate_periods import extract_rate_periods, extend_rate_periods


class Energidataservice:
    """Danish Energi Data Service integration for hourly electricity rates.

    Reads rates from HA sensor attributes, applies tariff adjustments,
    and converts to per-minute rate dictionaries.
    """

    def fetch_energidataservice_rates(self, entity_id, adjust_key=None, periods_out=None):
        """
        Read Energi Data Service attributes, add tariffs, and expand to per-minute values
        across each 15-minute interval (matches the new feed).
        Optional periods_out retains source cadence before forecast padding.
        """
        data_all = []
        rate_data = {}

        if entity_id:
            if self.debug_enable:
                self.log(f"Fetch Energi Data Service rates from {entity_id}")

            use_cent = self.get_state_wrapper(entity_id=entity_id, attribute="use_cent")

            data_import_today = self.get_state_wrapper(entity_id=entity_id, attribute="raw_today")
            if data_import_today:
                data_all += data_import_today
            else:
                self.log(f"Warn: No Energi Data Service data in sensor {entity_id} attribute 'raw_today'")

            data_import_tomorrow = self.get_state_wrapper(entity_id=entity_id, attribute="raw_tomorrow")
            if data_import_tomorrow:
                data_all += data_import_tomorrow
            else:
                self.log(f"Warn: No Energi Data Service data in sensor {entity_id} attribute 'raw_tomorrow'")

            tariffs = self.get_state_wrapper(entity_id=entity_id, attribute="tariffs") or {}

        if data_all:
            # Sort to be safe
            data_all.sort(key=lambda e: self._parse_iso(e.get("hour")) or datetime.min)

            # Add tariffs (HH:MM → H → HH → raw ISO)
            for entry in data_all:
                start_time_str = entry.get("hour")
                tariff = self._tariff_for(tariffs, start_time_str)
                entry["price_with_tariff"] = entry.get("price", 0) + tariff

            # Build per-minute map with 15-minute windows
            rate_data = self.minute_data_hourly_rates(
                data_all,
                self.forecast_days + 1,
                self.midnight_utc,
                rate_key="price_with_tariff",
                from_key="hour",
                adjust_key=adjust_key,
                scale=1.0,
                use_cent=use_cent,
                periods_out=periods_out,
                source_id=entity_id,
            )

        return rate_data

    def minute_data_hourly_rates(
        self,
        data,
        forecast_days,
        midnight_utc,
        rate_key,
        from_key,
        adjust_key=None,
        scale=1.0,
        use_cent=False,
        periods_out=None,
        source_id="energidataservice",
    ):
        """
        Convert 15-minute rate data into a per-minute dict keyed by minute offset from midnight_utc.
        FIXED: Handles timezone/DST correctly.
        Optional periods_out receives UTC bounds with cadence provenance.
        """
        rate_data = {}
        min_minute = -forecast_days * 24 * 60
        max_minute = forecast_days * 24 * 60
        interval_minutes = 15  # default granularity

        # Normalize midnight to naive (local comparison baseline)
        if midnight_utc.tzinfo is not None:
            midnight = midnight_utc.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            midnight = midnight_utc

        # Detect interval dynamically
        if len(data) >= 2:
            t0 = self._parse_iso(data[0].get(from_key))
            t1 = self._parse_iso(data[1].get(from_key))
            if t0 and t1:
                delta = int((t1 - t0).total_seconds() / 60)
                if 15 <= delta <= 60:
                    interval_minutes = delta

        extend_rate_periods(periods_out, extract_rate_periods(data, source_id, from_key=from_key, interval_minutes=interval_minutes, timezone_name="UTC"))

        for entry in data:
            start_time_str = entry.get(from_key)
            rate = entry.get(rate_key, 0) * scale

            if not use_cent:
                rate = rate * 100.0

            start_time = self._parse_iso(start_time_str)
            if start_time is None:
                self.log(f"Warn: Invalid time format '{start_time_str}' in data")
                continue

            # Normalize timezone properly
            if start_time.tzinfo is not None:
                start_time = start_time.astimezone(timezone.utc).replace(tzinfo=None)

            # Compute minute offset safely
            start_minute = int((start_time - midnight).total_seconds() / 60)
            end_minute = start_minute + interval_minutes

            # Fill each minute in interval
            for minute in range(start_minute, end_minute):
                if min_minute <= minute < max_minute:
                    rate_data[minute] = dp4(rate)

        # Fill missing minutes at the start of the data set without shifting timestamps
        if rate_data and 0 not in rate_data:
            min_key = min(rate_data.keys())
            if min_key > 0:
                first_rate = rate_data[min_key]
                for minute in range(0, min_key):
                    # Use the rate from 24 hours ahead if available, otherwise replicate the first valid rate
                    rate_data[minute] = rate_data.get(minute + 24 * 60, first_rate)

        if adjust_key:
            pass

        return rate_data

    # ---------- helpers ----------

    def _parse_iso(self, s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None

    def _tariff_for(self, tariffs, start_time_str):
        if not tariffs or not start_time_str:
            return 0

        dt = self._parse_iso(start_time_str)
        if not dt:
            return tariffs.get(str(start_time_str), 0)

        hhmm = f"{dt.hour:02d}:{dt.minute:02d}"
        h = str(dt.hour)
        hh = f"{dt.hour:02d}"

        return tariffs.get(hhmm, tariffs.get(h, tariffs.get(hh, tariffs.get(str(start_time_str), 0))))
