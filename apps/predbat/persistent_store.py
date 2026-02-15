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
Base class for persistent JSON file storage with backup and cleanup.
Provides common functionality for components needing to store state across restarts.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path


class PersistentStore:
    """
    Abstract base class for persistent JSON file storage.
    Handles load/save with backup, cleanup of old files, and automatic timestamping.
    """

    def __init__(self, base):
        """Initialize with reference to base PredBat instance"""
        self.base = base
        self.log = base.log

    def load(self, filepath):
        """
        Load data from JSON file with automatic backup restoration on corruption.

        Args:
            filepath: Path to JSON file to load

        Returns:
            Loaded data dict or None if file doesn't exist or is corrupted
        """
        try:
            if not os.path.exists(filepath):
                return None

            with open(filepath, 'r') as f:
                data = json.load(f)
                return data

        except (json.JSONDecodeError, IOError) as e:
            self.log(f"Warn: Failed to load {filepath}: {e}")

            # Try to restore from backup
            backup_path = filepath + '.bak'
            if os.path.exists(backup_path):
                try:
                    self.log(f"Warn: Attempting to restore from backup: {backup_path}")
                    with open(backup_path, 'r') as f:
                        data = json.load(f)
                    self.log(f"Warn: Successfully restored from backup")
                    return data
                except (json.JSONDecodeError, IOError) as e2:
                    self.log(f"Error: Backup restoration failed: {e2}")

            return None

    def save(self, filepath, data, backup=True):
        """
        Save data to JSON file with automatic backup and timestamp.

        Args:
            filepath: Path to JSON file to save
            data: Dict to save (will add last_updated timestamp)
            backup: Whether to backup existing file before overwrite

        Returns:
            True if successful, False otherwise
        """
        try:
            # Add timestamp
            data['last_updated'] = datetime.now().astimezone().isoformat()

            # Create directory if needed
            os.makedirs(os.path.dirname(filepath), exist_ok=True)

            # Backup existing file if requested
            if backup and os.path.exists(filepath):
                self.backup_file(filepath)

            # Write new file
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)

            # Cleanup old backups
            self.cleanup_backups(filepath)

            return True

        except (IOError, OSError) as e:
            self.log(f"Error: Failed to save {filepath}: {e}")
            return False

    def backup_file(self, filepath):
        """
        Create backup copy of file.

        Args:
            filepath: Path to file to backup
        """
        try:
            backup_path = filepath + '.bak'
            if os.path.exists(filepath):
                import shutil
                shutil.copy2(filepath, backup_path)
        except (IOError, OSError) as e:
            self.log(f"Warn: Failed to backup {filepath}: {e}")

    def cleanup_backups(self, filepath):
        """
        Remove backup files older than 1 day.

        Args:
            filepath: Path to main file (will check for .bak file)
        """
        try:
            backup_path = filepath + '.bak'
            if os.path.exists(backup_path):
                # Check file age
                file_time = datetime.fromtimestamp(os.path.getmtime(backup_path))
                age = datetime.now() - file_time

                if age > timedelta(days=1):
                    os.remove(backup_path)
                    self.log(f"Info: Cleaned up old backup: {backup_path}")

        except (IOError, OSError) as e:
            self.log(f"Warn: Failed to cleanup backup for {filepath}: {e}")

    def cleanup(self, directory, pattern, retention_days):
        """
        Remove files matching pattern older than retention period.

        Args:
            directory: Directory to search
            pattern: Glob pattern for files to cleanup
            retention_days: Number of days to retain files

        Returns:
            Number of files removed
        """
        try:
            if not os.path.exists(directory):
                return 0

            path = Path(directory)
            cutoff_time = datetime.now() - timedelta(days=retention_days)
            removed_count = 0

            for file_path in path.glob(pattern):
                try:
                    file_time = datetime.fromtimestamp(file_path.stat().st_mtime)
                    if file_time < cutoff_time:
                        file_path.unlink()
                        removed_count += 1
                        self.log(f"Info: Cleaned up old file: {file_path}")
                except (IOError, OSError) as e:
                    self.log(f"Warn: Failed to remove {file_path}: {e}")

            return removed_count

        except Exception as e:
            self.log(f"Error: Cleanup failed for {directory}/{pattern}: {e}")
            return 0

    def get_last_updated(self, filepath):
        """
        Get last_updated timestamp from JSON file.

        Args:
            filepath: Path to JSON file

        Returns:
            ISO 8601 timestamp string or None
        """
        data = self.load(filepath)
        if data and 'last_updated' in data:
            return data['last_updated']
        return None
