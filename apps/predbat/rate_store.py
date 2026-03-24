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
Persistent storage for import and export rates.
Stores the final computed rate tables from fetch, with past slots frozen
to prevent retrospective changes to historical cost calculations.
"""

import os
from datetime import datetime
from persistent_store import PersistentStore


class RateStore(PersistentStore):
    """
    Manages persistent storage of energy rate tables.

    Persists the final rate_import and rate_export dictionaries from fetch.
    Past slots (< minutes_now) are frozen once written and cannot be overwritten,
    ensuring accurate historical cost calculations even if future rates change
    or overrides disappear (IOG, Axle VPP).

    File structure: predbat_save/rates_YYYY_MM_DD.json
    Format: {"rates_import": {minute: rate, ...}, "rates_export": {minute: rate, ...}}
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

    def save_rates(self, date, rate_import, rate_export, freeze_before_minute):
        """
        Save rate tables, freezing past slots to prevent retrospective changes.

        Args:
            date: datetime object for the date
            rate_import: Dict of {minute: rate} for import rates
            rate_export: Dict of {minute: rate} for export rates
            freeze_before_minute: Minute offset - freeze all slots before this time

        Returns:
            True if successful
        """
        filepath = self._get_filepath(date)

        # Load existing data to preserve frozen slots
        existing_data = self.load(filepath)
        if existing_data is None:
            existing_data = {"rates_import": {}, "rates_export": {}}

        # Convert existing string keys to int keys for comparison
        existing_import = {int(k): v for k, v in existing_data.get("rates_import", {}).items()}
        existing_export = {int(k): v for k, v in existing_data.get("rates_export", {}).items()}

        # Build new data structure
        new_import = {}
        new_export = {}

        # Combine all minutes from both existing and new data
        all_minutes = set(existing_import.keys()) | set(rate_import.keys()) | set(existing_export.keys()) | set(rate_export.keys())

        for minute in sorted(all_minutes):
            # For past slots (frozen), use existing value if present, otherwise new value
            if minute < freeze_before_minute:
                if minute in existing_import:
                    new_import[str(minute)] = existing_import[minute]
                elif minute in rate_import:
                    new_import[str(minute)] = rate_import[minute]

                if minute in existing_export:
                    new_export[str(minute)] = existing_export[minute]
                elif minute in rate_export:
                    new_export[str(minute)] = rate_export[minute]
            else:
                # For current/future slots, always use new value
                if minute in rate_import:
                    new_import[str(minute)] = rate_import[minute]
                if minute in rate_export:
                    new_export[str(minute)] = rate_export[minute]

        data = {
            "rates_import": new_import,
            "rates_export": new_export,
            "last_updated": datetime.now().isoformat(),
            "frozen_before_minute": freeze_before_minute
        }

        return self.save(filepath, data, backup=True)

    def load_rates(self, date):
        """
        Load stored rate tables for given date.

        Args:
            date: datetime object for date to load

        Returns:
            Tuple of (rate_import_dict, rate_export_dict) or (None, None) if no file
        """
        filepath = self._get_filepath(date)
        data = self.load(filepath)

        if data is None:
            return None, None

        # Convert string keys back to integers
        rate_import = {int(k): v for k, v in data.get("rates_import", {}).items()}
        rate_export = {int(k): v for k, v in data.get("rates_export", {}).items()}

        return rate_import, rate_export

    def cleanup_old_files(self, retention_days):
        """
        Remove rate files older than retention period.

        Args:
            retention_days: Number of days to retain files

        Returns:
            Number of files removed
        """
        return self.cleanup(self.save_dir, "rates_*.json", retention_days)
