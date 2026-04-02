from datetime import datetime
from utils import dp4


class Stromligning:
    def fetch_stromligning_rates(self, entity_id_today, entity_id_tomorrow, entity_id_today_export=None, entity_id_tomorrow_export=None, adjust_key=None):
        """
        Read Strømligning attributes from 4 sensors (today/tomorrow for import/export).
        Strømligning provides 15-minute intervals with price, start, and end times.

        Args:
            entity_id_today: Sensor for today's import prices
            entity_id_tomorrow: Sensor for tomorrow's import prices
            entity_id_today_export: Sensor for today's export prices (optional)
            entity_id_tomorrow_export: Sensor for tomorrow's export prices (optional)
            adjust_key: Optional key for adjustments

        Returns:
            dict: Per-minute rate data keyed by minute offset from midnight_utc
        """
        data_all = []
        rate_data = {}
        scale = 1.0

        if entity_id_today or entity_id_tomorrow:
            if self.debug_enable:
                self.log(f"Fetch Strømligning rates from {entity_id_today} and {entity_id_tomorrow}")

            # Fetch today's data
            if entity_id_today:
                unit_today = self.get_state_wrapper(entity_id=entity_id_today, attribute="unit_of_measurement")
                data_import_today = self.get_state_wrapper(entity_id=entity_id_today, attribute="prices_today")
                if not data_import_today:
                    # Some integrations expose a single `prices` list for current day.
                    data_import_today = self.get_state_wrapper(entity_id=entity_id_today, attribute="prices")
                if data_import_today:
                    data_all += data_import_today
                else:
                    self.log(f"Warn: No Strømligning data in sensor {entity_id_today} attributes 'prices_today' or 'prices'")

            # Fetch tomorrow's data
            if entity_id_tomorrow:
                unit_tomorrow = self.get_state_wrapper(entity_id=entity_id_tomorrow, attribute="unit_of_measurement")
                data_import_tomorrow = self.get_state_wrapper(entity_id=entity_id_tomorrow, attribute="prices_tomorrow")
                if not data_import_tomorrow:
                    # Compatibility fallback if only a generic list exists.
                    data_import_tomorrow = self.get_state_wrapper(entity_id=entity_id_tomorrow, attribute="prices")
                if data_import_tomorrow:
                    data_all += data_import_tomorrow
                else:
                    self.log(f"Warn: No Strømligning data in sensor {entity_id_tomorrow} attributes 'prices_tomorrow' or 'prices'")

            unit = (unit_today if entity_id_today else None) or (unit_tomorrow if entity_id_tomorrow else None) or ""
            if isinstance(unit, str) and "kr/" in unit.lower():
                # PredBat internal rate units are ore/cents for compatibility with other providers.
                scale = 100.0

        if data_all:
            # Sort to be safe
            data_all.sort(key=lambda e: self._parse_iso(e.get("start")) or datetime.min)

            # Build per-minute map with 15-minute windows
            rate_data = self._minute_data_stromligning_rates(
                data_all,
                self.forecast_days + 1,
                self.midnight_utc,
                scale=scale,
                adjust_key=adjust_key,
            )

        return rate_data

    def _minute_data_stromligning_rates(self, data, forecast_days, midnight_utc, scale=1.0, adjust_key=None):
        """
        Convert 15-minute Strømligning rate data into a per-minute dict keyed by minute offset from midnight_utc.

        Strømligning data format:
        - price: Price value (already in correct unit)
        - start: ISO timestamp for interval start
        - end: ISO timestamp for interval end
        """
        rate_data = {}
        min_minute = -forecast_days * 24 * 60
        max_minute = forecast_days * 24 * 60

        for entry in data:
            start_time_str = entry.get("start")
            end_time_str = entry.get("end")
            rate = entry.get("price", 0) * scale

            # Parse times robustly
            start_time = self._parse_iso(start_time_str)
            end_time = self._parse_iso(end_time_str)

            if start_time is None or end_time is None:
                self.log(f"Warn: Invalid time format in Strømligning data: start='{start_time_str}', end='{end_time_str}'")
                continue

            # Calculate minute offsets from midnight_utc
            try:
                start_minute = int((start_time - midnight_utc).total_seconds() / 60)
                end_minute = int((end_time - midnight_utc).total_seconds() / 60)
            except TypeError:
                # If midnight_utc is naive, drop tzinfo from start_time for subtraction
                start_minute = int((start_time.replace(tzinfo=None) - midnight_utc).total_seconds() / 60)
                end_minute = int((end_time.replace(tzinfo=None) - midnight_utc).total_seconds() / 60)

            # Some feeds express 23:00-00:00 with end on the same date; treat as next day.
            if end_minute <= start_minute:
                end_minute += 24 * 60

            # Fill each minute in the interval
            for minute in range(start_minute, end_minute):
                if min_minute <= minute < max_minute:
                    rate_data[minute] = dp4(rate)

        if adjust_key:
            # Hook for intelligent adjustments (can be implemented later if needed)
            pass

        return rate_data

    # ---------- helpers ----------

    def _parse_iso(self, s):
        """Parse ISO format timestamp string."""
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except Exception:
            return None
