# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Persistent storage for import and export rates with finalization logic.
Stores rates at the time they are first retrieved and applies overrides separately,
preventing retrospective changes to historical cost calculations.
"""

import os
from datetime import datetime, timedelta
from persistent_store import PersistentStore


class RateStore(PersistentStore):
    """
    Manages persistent storage of energy rates with slot-based structure.

    Stores rates in 30-minute slots (configurable via plan_interval_minutes) with:
    - initial: Base rate from API at first retrieval
    - automatic: Override from external services (IOG, Axle, saving sessions)
    - manual: User override from manual selectors
    - finalized: Lock flag set 5 minutes past slot start time

    File structure: predbat_save/rates_YYYY_MM_DD.json
    """

    def __init__(self, base, save_dir="predbat_save"):
        """
        Initialize rate store.

        Args:
            base: PredBat instance
            save_dir: Directory for rate files (relative to workspace root)
        """
        super().__init__(base)
        self.save_dir = save_dir
        self.plan_interval_minutes = base.plan_interval_minutes

        # In-memory cache of loaded rate files
        # Key: date string "YYYY-MM-DD", Value: rate data dict
        self.rate_cache = {}

        # Load and finalize rates for today and yesterday
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        self.load_rates(today)
        self.load_rates(yesterday)

        # Finalize past slots
        finalized_today = self.finalize_slots(today, base.minutes_now)
        finalized_yesterday = self.finalize_slots(yesterday, 24 * 60)  # Finalize all yesterday slots
        if finalized_today > 0 or finalized_yesterday > 0:
            self.log("Finalized {} slots for today and {} slots for yesterday".format(finalized_today, finalized_yesterday))

        # Cleanup old rate files
        retention_days = base.get_arg("rate_retention_days", 7)
        removed = self.cleanup_old_files(retention_days)
        if removed > 0:
            self.log("Cleaned up {} old rate files".format(removed))

    def _get_filepath(self, date):
        """
        Get filepath for rate file for given date.

        Args:
            date: datetime object

        Returns:
            Full path string to rate JSON file
        """
        date_str = date.strftime("%Y_%m_%d")
        filename = f"rates_{date_str}.json"
        return os.path.join(self.save_dir, filename)

    def _get_date_key(self, date):
        """Get cache key for date"""
        return date.strftime("%Y-%m-%d")

    def _minutes_to_time(self, minutes):
        """
        Convert minute offset from midnight to HH:MM string.

        Args:
            minutes: Minutes since midnight

        Returns:
            Time string in format "HH:MM"
        """
        hours = int(minutes // 60)
        mins = int(minutes % 60)
        return f"{hours:02d}:{mins:02d}"

    def _time_to_minutes(self, time_str):
        """
        Convert HH:MM string to minute offset from midnight.

        Args:
            time_str: Time in format "HH:MM"

        Returns:
            Minutes since midnight as int
        """
        parts = time_str.split(':')
        return int(parts[0]) * 60 + int(parts[1])

    def _get_slot_start(self, minutes):
        """
        Get slot start time for given minute offset.
        Uses same calculation as output.py line 997.

        Args:
            minutes: Minute offset from midnight

        Returns:
            Slot start minute offset
        """
        return int(minutes / self.plan_interval_minutes) * self.plan_interval_minutes

    def _init_empty_structure(self):
        """
        Create empty rate data structure.

        Returns:
            Dict with plan_interval_minutes and empty import/export rate dicts
        """
        return {
            'plan_interval_minutes': self.plan_interval_minutes,
            'rates_import': {},
            'rates_export': {}
        }

    def _init_empty_slot(self):
        """
        Create empty slot structure.

        Returns:
            Dict with initial/automatic/manual/finalized fields
        """
        return {
            'initial': None,
            'automatic': None,
            'manual': None,
            'finalized': False
        }

    def load_rates(self, date):
        """
        Load rate data for given date into cache.

        Args:
            date: datetime object for date to load

        Returns:
            Rate data dict or None if file doesn't exist
        """
        date_key = self._get_date_key(date)

        # Check if already cached
        if date_key in self.rate_cache:
            return self.rate_cache[date_key]

        # Load from file
        filepath = self._get_filepath(date)
        data = self.load(filepath)

        if data is None:
            # Initialize empty structure
            data = self._init_empty_structure()

        # Validate plan_interval_minutes matches
        if 'plan_interval_minutes' in data:
            if data['plan_interval_minutes'] != self.plan_interval_minutes:
                self.log(f"Error: Rate file {filepath} has plan_interval_minutes={data['plan_interval_minutes']} but current config is {self.plan_interval_minutes}. Creating backup and starting fresh.")
                # Backup old file
                self.backup_file(filepath)
                # Start with empty structure
                data = self._init_empty_structure()
        else:
            # Old file format, add field
            data['plan_interval_minutes'] = self.plan_interval_minutes

        # Ensure structure exists
        if 'rates_import' not in data:
            data['rates_import'] = {}
        if 'rates_export' not in data:
            data['rates_export'] = {}

        # Cache it
        self.rate_cache[date_key] = data

        return data

    def save_rates(self, date):
        """
        Save rate data for given date from cache to file.

        Args:
            date: datetime object for date to save

        Returns:
            True if successful
        """
        date_key = self._get_date_key(date)

        if date_key not in self.rate_cache:
            self.log(f"Warn: No rate data in cache for {date_key}")
            return False

        data = self.rate_cache[date_key]
        filepath = self._get_filepath(date)

        return self.save(filepath, data, backup=True)

    def write_base_rate(self, date, minute, rate_import, rate_export):
        """
        Write initial base rate for a slot (only if not already set).
        This captures the rate at first retrieval from API.

        Args:
            date: datetime object for the date
            minute: Minute offset from midnight
            rate_import: Import rate value
            rate_export: Export rate value
        """
        # Load rate data
        data = self.load_rates(date)

        # Get slot start
        slot_start = self._get_slot_start(minute)
        slot_time = self._minutes_to_time(slot_start)

        # Initialize slots if needed
        if slot_time not in data['rates_import']:
            data['rates_import'][slot_time] = self._init_empty_slot()
        if slot_time not in data['rates_export']:
            data['rates_export'][slot_time] = self._init_empty_slot()

        # Only write initial rate if not already set
        if data['rates_import'][slot_time]['initial'] is None:
            data['rates_import'][slot_time]['initial'] = rate_import

        if data['rates_export'][slot_time]['initial'] is None:
            data['rates_export'][slot_time]['initial'] = rate_export

        # Save immediately
        self.save_rates(date)

    def update_auto_override(self, date, minute, rate_import, rate_export, source):
        """
        Update automatic override rate for a slot (IOG, Axle, saving sessions).
        Only updates non-finalized slots.

        Args:
            date: datetime object for the date
            minute: Minute offset from midnight
            rate_import: Import rate value or None to clear
            rate_export: Export rate value or None to clear
            source: String identifying override source (e.g., "IOG", "Axle")
        """
        # Load rate data
        data = self.load_rates(date)

        # Get slot start
        slot_start = self._get_slot_start(minute)
        slot_time = self._minutes_to_time(slot_start)

        # Initialize slots if needed
        if slot_time not in data['rates_import']:
            data['rates_import'][slot_time] = self._init_empty_slot()
        if slot_time not in data['rates_export']:
            data['rates_export'][slot_time] = self._init_empty_slot()

        # Check if slot is finalized
        if data['rates_import'][slot_time]['finalized']:
            # Don't modify finalized slots
            return

        # Store override with source tracking
        import_slot = data['rates_import'][slot_time]
        export_slot = data['rates_export'][slot_time]

        if rate_import is not None:
            import_slot['automatic'] = {
                'rate': rate_import,
                'source': source
            }
        else:
            # Clear override
            import_slot['automatic'] = None

        if rate_export is not None:
            export_slot['automatic'] = {
                'rate': rate_export,
                'source': source
            }
        else:
            # Clear override
            export_slot['automatic'] = None

        # Save immediately
        self.save_rates(date)

    def update_manual_override(self, date, minute, rate_import, rate_export):
        """
        Update manual override rate for a slot (from user selectors).
        Only updates non-finalized slots.

        Args:
            date: datetime object for the date
            minute: Minute offset from midnight
            rate_import: Import rate value or None to clear
            rate_export: Export rate value or None to clear
        """
        # Load rate data
        data = self.load_rates(date)

        # Get slot start
        slot_start = self._get_slot_start(minute)
        slot_time = self._minutes_to_time(slot_start)

        # Initialize slots if needed
        if slot_time not in data['rates_import']:
            data['rates_import'][slot_time] = self._init_empty_slot()
        if slot_time not in data['rates_export']:
            data['rates_export'][slot_time] = self._init_empty_slot()

        # Check if slot is finalized
        if data['rates_import'][slot_time]['finalized']:
            # Don't modify finalized slots
            return

        # Store manual override
        data['rates_import'][slot_time]['manual'] = rate_import
        data['rates_export'][slot_time]['manual'] = rate_export

        # Save immediately
        self.save_rates(date)

    def finalize_slots(self, date, current_minute):
        """
        Finalize all slots that have passed their start time by 5+ minutes.
        Finalized slots cannot be modified by overrides.

        Args:
            date: datetime object for the date
            current_minute: Current minute offset from midnight

        Returns:
            Number of slots finalized
        """
        # Load rate data
        data = self.load_rates(date)

        finalized_count = 0

        # Process all slots
        for slot_time in data['rates_import'].keys():
            slot_minute = self._time_to_minutes(slot_time)

            # Check if slot should be finalized
            # Finalize if current time is 5+ minutes past slot start
            if current_minute >= slot_minute + 5:
                if not data['rates_import'][slot_time]['finalized']:
                    data['rates_import'][slot_time]['finalized'] = True
                    data['rates_export'][slot_time]['finalized'] = True
                    finalized_count += 1

        if finalized_count > 0:
            self.save_rates(date)

        return finalized_count

    def get_rate(self, date, minute, is_import=True):
        """
        Get effective rate for a given time.
        Returns manual override > automatic override > initial rate > 0.

        Args:
            date: datetime object for the date
            minute: Minute offset from midnight
            is_import: True for import rate, False for export rate

        Returns:
            Rate value (float) or 0 if not found
        """
        # Load rate data
        data = self.load_rates(date)

        # Get slot start
        slot_start = self._get_slot_start(minute)
        slot_time = self._minutes_to_time(slot_start)

        # Select import or export rates
        rates = data['rates_import'] if is_import else data['rates_export']

        if slot_time not in rates:
            return 0

        slot = rates[slot_time]

        # Priority: manual > automatic > initial > 0
        if slot['manual'] is not None:
            return slot['manual']

        if slot['automatic'] is not None:
            # Handle dict format with source tracking
            if isinstance(slot['automatic'], dict):
                return slot['automatic']['rate']
            return slot['automatic']

        if slot['initial'] is not None:
            return slot['initial']

        return 0

    def get_automatic_rate(self, date, minute, is_import=True):
        """
        Get the automatic override rate (ignoring manual overrides).
        Used for displaying what automatic systems are doing.

        Args:
            date: datetime object for the date
            minute: Minute offset from midnight
            is_import: True for import rate, False for export rate

        Returns:
            Rate value (float) or None if no automatic override
        """
        # Load rate data
        data = self.load_rates(date)

        # Get slot start
        slot_start = self._get_slot_start(minute)
        slot_time = self._minutes_to_time(slot_start)

        # Select import or export rates
        rates = data['rates_import'] if is_import else data['rates_export']

        if slot_time not in rates:
            return None

        slot = rates[slot_time]

        # Return automatic override if set
        if slot['automatic'] is not None:
            # Handle dict format with source tracking
            if isinstance(slot['automatic'], dict):
                return slot['automatic']['rate']
            return slot['automatic']

        # Fall back to initial rate
        if slot['initial'] is not None:
            return slot['initial']

        return None

    def cleanup_old_files(self, retention_days):
        """
        Remove rate files older than retention period.

        Args:
            retention_days: Number of days to retain files

        Returns:
            Number of files removed
        """
        return self.cleanup(self.save_dir, "rates_*.json", retention_days)
