"""Danish Energi Data Service integration for electricity rates.

Reads hourly electricity rates from the Energi Data Service Home Assistant
integration sensor, applies tariff adjustments, and converts to per-minute
rate dictionaries.
"""

from datetime import datetime
from utils import dp4


class Energidataservice:
    """Danish Energi Data Service integration for hourly electricity rates.

    Reads rates from HA sensor attributes, applies tariff adjustments,
    and converts to per-minute rate dictionaries.
    """

    def fetch_energidataservice_rates(self, entity_id, adjust_key=None):
        """
        Read Energi Data Service attributes, add tariffs, and expand to per-minute values
        across each 15-minute interval (matches the new feed).
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
            )

        return rate_data

    def minute_data_hourly_rates(self, data, forecast_days, midnight_utc, rate_key, from_key, adjust_key=None, scale=1.0, use_cent=False):
        """
        Convert 15-minute rate data into a per-minute dict keyed by minute offset from midnight_utc.
        """
        rate_data = {}
        min_minute = -forecast_days * 24 * 60
        max_minute = forecast_days * 24 * 60
        interval_minutes = 15  # new feed granularity

        # Find gap between two entries in minutes
        if len(data) < 2:
            pass
        else:
            t0 = self._parse_iso(data[0].get(from_key))
            t1 = self._parse_iso(data[1].get(from_key))
            if t0 and t1:
                interval_minutes = int((t1 - t0).total_seconds() / 60)
                if interval_minutes <= 15 or interval_minutes > 60:
                    interval_minutes = 15

        for entry in data:
            start_time_str = entry.get(from_key)
            rate = entry.get(rate_key, 0) * scale
            if not use_cent:
                # Keep behavior: convert DKK → øre (or cents) if use_cent is False
                rate = rate * 100.0

            # Parse time robustly
            start_time = self._parse_iso(start_time_str)
            if start_time is None:
                self.log(f"Warn: Invalid time format '{start_time_str}' in data")
                continue

            # Support naive/aware midnight_utc gracefully
            try:
                start_minute = int((start_time - midnight_utc).total_seconds() / 60)
            except TypeError:
                # If midnight_utc is naive, drop tzinfo from start_time for subtraction
                start_minute = int((start_time.replace(tzinfo=None) - midnight_utc).total_seconds() / 60)

            end_minute = start_minute + interval_minutes

            # Fill each minute in the 15-min slot
            for minute in range(start_minute, end_minute):
                if min_minute <= minute < max_minute:
                    rate_data[minute] = dp4(rate)

        if adjust_key:
            # hook for intelligent adjustments
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
        s = str(start_time_str)
        dt = self._parse_iso(s)
        if not dt:
            return tariffs.get(s, 0)
        hhmm = f"{dt.hour:02d}:{dt.minute:02d}"  # 08:15
        h = str(dt.hour)  # "8"
        hh = f"{dt.hour:02d}"  # "08"
        return tariffs.get(hhmm, tariffs.get(h, tariffs.get(hh, tariffs.get(s, 0))))
