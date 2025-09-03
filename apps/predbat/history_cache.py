"""
Simple history cache for Home Assistant data
"""

import threading
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional, Any


class HistoryCache:
    """Simple in-memory cache for Home Assistant history data"""

    def __init__(self):
        self.cache_lock = threading.RLock()
        # Cache structure: {entity_id: {"data": deque([history_items]), "latest": datetime}}
        self.cache_data: Dict[str, Dict[str, Any]] = {}
        self.enabled = False

    def configure(self, enabled: bool):
        """Configure the cache"""
        self.enabled = enabled
        if not enabled:
            with self.cache_lock:
                self.cache_data.clear()

    def _get_timestamp(self, item: Dict[str, Any]) -> Optional[datetime]:
        """Extract timestamp from history item"""
        if not isinstance(item, dict):
            return None

        timestamp_str = item.get("last_changed")
        if timestamp_str:
            try:
                return datetime.fromisoformat(timestamp_str)
            except (ValueError, TypeError):
                pass
        return None

    def get_or_fetch(self, entity_id: str, start_time: datetime, end_time: datetime,
                     fetch_func) -> Optional[List[Dict]]:
        """Get cached data or fetch missing data using provided function."""
        if not self.enabled:
            return fetch_func(start_time, end_time)

        entity_key = entity_id.lower()

        fetch_start_time = None
        with self.cache_lock:
            cache_entry = self.cache_data.get(entity_key)

            if not cache_entry:
                fetch_start_time = start_time
            else:
                latest_time = cache_entry.get("latest")
                if latest_time is None or latest_time < end_time:
                    fetch_start_time = latest_time or start_time

        if fetch_start_time:
            new_data = fetch_func(fetch_start_time, end_time)
            if new_data:
                self.update_cache(entity_id, new_data)

        with self.cache_lock:
            cache_entry = self.cache_data.get(entity_key)
            if not cache_entry:
                return []

            # Prune old data from cache in-place. We assume that start_time
            # is consistent each time we're called for a specific entity
            while cache_entry["data"]:
                ts = self._get_timestamp(cache_entry["data"][0])
                if ts and ts < start_time:
                    cache_entry["data"].popleft()
                else:
                    break

            # Return filtered cached data
            return [item for item in cache_entry["data"]
                    if (ts := self._get_timestamp(item)) and start_time <= ts <= end_time]

    def update_cache(self, entity_id: str, new_data: List[Dict]):
        """Update cache with new data, assuming new_data is chronologically sorted and newer than existing data."""
        if not self.enabled or not new_data:
            return

        # The HA API can return data in a nested list, e.g., [[item1, item2]].
        # This flattens it to [item1, item2] for consistent processing.
        if isinstance(new_data, list) and len(new_data) > 0 and isinstance(new_data[0], list):
            new_data = new_data[0]

        with self.cache_lock:
            entity_key = entity_id.lower()

            if entity_key not in self.cache_data:
                self.cache_data[entity_key] = {"data": deque(), "latest": None}

            cache_entry = self.cache_data[entity_key]
            existing_data: Deque[Dict] = cache_entry["data"]

            existing_data.extend(new_data)

            # Update the latest timestamp from the last item in the new data
            if new_data:
                latest_item = new_data[-1]
                if (timestamp := self._get_timestamp(latest_item)):
                    if cache_entry["latest"] is None or timestamp > cache_entry["latest"]:
                        cache_entry["latest"] = timestamp
