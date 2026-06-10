# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Storage component for Predbat.

Provides an abstract base class StorageBase and a local filesystem implementation
StorageLocalFiles, wrapped in StorageComponent for lifecycle management.

Files are stored under config_root/cache/ as:
  {module}_{filename}.{ext}   — data file (yaml/json/txt)
  {module}_{filename}.meta    — JSON sidecar with format, expiry, module, created
"""

import json
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import aiofiles
import yaml

from component_base import ComponentBase


STORAGE_FORMATS = ("yaml", "json", "text")
STORAGE_EXTENSIONS = {"yaml": "yaml", "json": "json", "text": "txt"}


def _parse_dt_utc(value):
    """Parse an ISO-format datetime string and return a timezone-aware UTC datetime.

    If the parsed value is naive (no tzinfo), it is assumed to be UTC.
    Returns None if parsing fails.
    """
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_\-]")


def _safe_name(value):
    """Sanitise a module or filename identifier for safe use in a file path.

    Replaces any character that is not alphanumeric, underscore, or hyphen with
    an underscore. This prevents path-traversal attacks via ``..`` or path
    separators embedded in the identifier.
    """
    return _SAFE_NAME_RE.sub("_", value)


class StorageBase(ABC):
    """Abstract base class for storage backends."""

    @abstractmethod
    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Save data for a given module and filename.

        Args:
            module: The calling module name (e.g. 'solis')
            filename: Logical filename identifier
            data: Data to save (will be serialised according to format)
            format: One of 'yaml', 'json', or 'text'
            expiry: Optional datetime (timezone-aware) after which the data is considered stale

        Returns:
            True on success, False on failure
        """
        pass

    @abstractmethod
    async def load(self, module, filename):
        """Load data for a given module and filename.

        Args:
            module: The calling module name
            filename: Logical filename identifier

        Returns:
            Deserialised data, or None if missing or expired
        """
        pass

    @abstractmethod
    async def age(self, module, filename):
        """Return the age of stored data in minutes since it was created.

        Args:
            module: The calling module name
            filename: Logical filename identifier

        Returns:
            Age in minutes as a float, or None if the entry does not exist
        """
        pass

    @abstractmethod
    async def cleanup(self):
        """Delete all expired cached files."""
        pass

    async def _acquire_refresh_lock(self, module, filename):
        """Try to acquire a refresh lock for a key. Default: always succeeds (no coordination).

        Args:
            module: The calling module name
            filename: Logical filename identifier

        Returns:
            True if the caller should perform the refresh.
        """
        return True

    async def _release_refresh_lock(self, module, filename):
        """Release a refresh lock. Default: no-op.

        Args:
            module: The calling module name
            filename: Logical filename identifier
        """
        return None

    async def fetch_cached(self, module, filename, fetch_fn, fresh_minutes=30, stale_minutes=35, format="yaml"):
        """Return cached data if fresh; serve stale while one caller refreshes; else fetch and store.

        Args:
            module: The calling module name
            filename: Logical filename identifier
            fetch_fn: Zero-arg coroutine function that fetches fresh data
            fresh_minutes: Below this age the cached value is returned without refresh
            stale_minutes: Between fresh_minutes and this, serve stale while one caller refreshes
            format: One of 'yaml', 'json', or 'text'

        Returns:
            Cached or freshly fetched data, or None if nothing could be obtained
        """
        age_minutes = await self.age(module, filename)

        if age_minutes is not None and age_minutes < fresh_minutes:
            cached = await self.load(module, filename)
            if cached is not None:
                return cached
            # Entry exists but is expired/unreadable; treat as a miss and re-fetch.
            age_minutes = None
        if age_minutes is not None and age_minutes < stale_minutes:
            cached = await self.load(module, filename)
            if await self._acquire_refresh_lock(module, filename):
                try:
                    data = await fetch_fn()
                    if data is not None:
                        await self.save(module, filename, data, format=format)
                        return data
                except Exception as e:  # pylint: disable=broad-except
                    if hasattr(self, "log") and callable(getattr(self, "log")):
                        self.log("Storage: Warn: refresh failed for {}/{}, serving cached: {}".format(module, filename, e))
                finally:
                    await self._release_refresh_lock(module, filename)
            return cached

        try:
            data = await fetch_fn()
        except Exception as e:  # pylint: disable=broad-except
            if hasattr(self, "log") and callable(getattr(self, "log")):
                self.log("Storage: Warn: fetch failed for {}/{}: {}".format(module, filename, e))
            data = None
        if data is not None:
            await self.save(module, filename, data, format=format)
            return data
        return await self.load(module, filename)


class StorageLocalFiles(StorageBase):
    """Local filesystem storage backend.

    Stores files in config_root/cache as {module}_{filename}.{ext} with a JSON
    sidecar {module}_{filename}.meta containing format, expiry, module, and created.
    """

    def __init__(self, config_root, log):
        """Initialise the local file storage backend.

        Args:
            config_root: Root configuration directory path
            log: Logging function
        """
        self.cache_path = os.path.join(config_root, "cache")
        self.log = log
        os.makedirs(self.cache_path, exist_ok=True)

    def _data_path(self, module, filename, fmt):
        """Return the full path for a data file."""
        ext = STORAGE_EXTENSIONS.get(fmt, fmt)
        return os.path.join(self.cache_path, "{}_{}.{}".format(_safe_name(module), _safe_name(filename), ext))

    def _meta_path(self, module, filename):
        """Return the full path for the metadata sidecar."""
        return os.path.join(self.cache_path, "{}_{}.meta".format(_safe_name(module), _safe_name(filename)))

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Save data to disk with a JSON metadata sidecar.

        Args:
            module: The calling module name
            filename: Logical filename identifier
            data: Data to serialise and write
            format: One of 'yaml', 'json', or 'text'
            expiry: Optional timezone-aware datetime after which data is stale

        Returns:
            True on success, False on failure
        """
        if format not in STORAGE_FORMATS:
            self.log("Storage: Warn: Unknown format '{}' for {}/{}, defaulting to yaml".format(format, module, filename))
            format = "yaml"

        try:
            if format == "yaml":
                text = yaml.safe_dump(data)
            elif format == "json":
                text = json.dumps(data)
            else:
                text = str(data)
        except (yaml.YAMLError, TypeError, ValueError) as e:
            self.log("Storage: Error: Failed to serialise {}/{}: {}".format(module, filename, e))
            return False

        data_path = self._data_path(module, filename, format)
        try:
            async with aiofiles.open(data_path, "w") as f:
                await f.write(text)
        except IOError as e:
            self.log("Storage: Error: Failed to write {}: {}".format(data_path, e))
            return False

        if expiry is not None and expiry.tzinfo is None:
            self.log("Storage: Warn: Naive expiry datetime for {}/{} — assuming UTC".format(module, filename))
            expiry = expiry.replace(tzinfo=timezone.utc)

        meta = {
            "format": format,
            "expiry": expiry.isoformat() if expiry is not None else None,
            "module": module,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        meta_path = self._meta_path(module, filename)
        try:
            async with aiofiles.open(meta_path, "w") as f:
                await f.write(json.dumps(meta))
        except IOError as e:
            self.log("Storage: Error: Failed to write metadata {}: {}".format(meta_path, e))
            # Remove the data file so we don't leave an unreadable orphan
            try:
                os.remove(data_path)
            except OSError:
                pass
            return False

        return True

    async def load(self, module, filename):
        """Load data from disk, checking metadata for expiry.

        Args:
            module: The calling module name
            filename: Logical filename identifier

        Returns:
            Deserialised data, or None if missing or expired
        """
        meta_path = self._meta_path(module, filename)

        if not os.path.exists(meta_path):
            return None

        try:
            async with aiofiles.open(meta_path, "r") as f:
                meta_text = await f.read()
            meta = json.loads(meta_text)
        except (IOError, json.JSONDecodeError) as e:
            self.log("Storage: Warn: Failed to read metadata {}: {}".format(meta_path, e))
            return None

        expiry_str = meta.get("expiry")
        if expiry_str is not None:
            expiry_dt = _parse_dt_utc(expiry_str)
            if expiry_dt is not None and datetime.now(timezone.utc) > expiry_dt:
                return None

        fmt = meta.get("format", "yaml")
        if fmt not in STORAGE_FORMATS:
            self.log("Storage: Warn: Unknown format '{}' in metadata for {}/{}, defaulting to yaml".format(fmt, module, filename))
            fmt = "yaml"
        data_path = self._data_path(module, filename, fmt)

        if not os.path.exists(data_path):
            return None

        try:
            async with aiofiles.open(data_path, "r") as f:
                text = await f.read()
        except IOError as e:
            self.log("Storage: Warn: Failed to read {}: {}".format(data_path, e))
            return None

        try:
            if fmt == "yaml":
                return yaml.safe_load(text)
            elif fmt == "json":
                return json.loads(text)
            else:
                return text
        except (yaml.YAMLError, json.JSONDecodeError) as e:
            self.log("Storage: Warn: Failed to deserialise {}: {}".format(data_path, e))
            return None

    async def age(self, module, filename):
        """Return the age of stored data in minutes since it was created.

        Args:
            module: The calling module name
            filename: Logical filename identifier

        Returns:
            Age in minutes as a float, or None if the entry does not exist
        """
        meta_path = self._meta_path(module, filename)

        if not os.path.exists(meta_path):
            return None

        try:
            async with aiofiles.open(meta_path, "r") as f:
                meta_text = await f.read()
            meta = json.loads(meta_text)
        except (IOError, json.JSONDecodeError):
            return None

        created_str = meta.get("created")
        if not created_str:
            return None

        try:
            created_dt = _parse_dt_utc(created_str)
            if created_dt is None:
                return None
            return (datetime.now(timezone.utc) - created_dt).total_seconds() / 60.0
        except (ValueError, TypeError):
            return None

    async def cleanup(self):
        """Delete expired data and metadata files from the cache directory."""
        if not os.path.exists(self.cache_path):
            return

        now = datetime.now(timezone.utc)
        for entry in os.scandir(self.cache_path):
            if not entry.name.endswith(".meta"):
                continue

            meta_path = entry.path
            try:
                async with aiofiles.open(meta_path, "r") as f:
                    meta_text = await f.read()
                meta = json.loads(meta_text)
            except (IOError, json.JSONDecodeError):
                continue

            expiry_str = meta.get("expiry")
            if expiry_str is None:
                continue

            try:
                expiry_dt = _parse_dt_utc(expiry_str)
                if expiry_dt is None:
                    continue
            except (ValueError, TypeError):
                continue

            if now <= expiry_dt:
                continue

            module = meta.get("module", "")
            fmt = meta.get("format", "yaml")
            if fmt not in STORAGE_FORMATS:
                fmt = "yaml"
            stem = entry.name[: -len(".meta")]
            ext = STORAGE_EXTENSIONS.get(fmt, fmt)
            data_path = os.path.join(self.cache_path, "{}.{}".format(stem, ext))

            try:
                if os.path.exists(data_path):
                    os.remove(data_path)
            except OSError as e:
                self.log("Storage: Warn: Failed to delete {}: {}".format(data_path, e))

            try:
                os.remove(meta_path)
            except OSError as e:
                self.log("Storage: Warn: Failed to delete {}: {}".format(meta_path, e))

            self.log("Storage: Cleaned up expired cache file {} (module={})".format(stem, module))


class StorageComponent(ComponentBase):
    """Storage component providing save/load access to a cache backend.

    All other components can access it via self.storage from ComponentBase.
    """

    def initialize(self, **kwargs):
        """Initialise the storage component with a local filesystem backend."""
        self.backend = StorageLocalFiles(self.config_root, self.log)

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Save data via the storage backend.

        Args:
            module: The calling module name
            filename: Logical filename identifier
            data: Data to save
            format: One of 'yaml', 'json', or 'text'
            expiry: Optional timezone-aware expiry datetime

        Returns:
            True on success, False on failure
        """
        return await self.backend.save(module, filename, data, format=format, expiry=expiry)

    async def load(self, module, filename):
        """Load data via the storage backend.

        Args:
            module: The calling module name
            filename: Logical filename identifier

        Returns:
            Deserialised data, or None if missing or expired
        """
        return await self.backend.load(module, filename)

    async def age(self, module, filename):
        """Return the age of stored data in minutes since it was created.

        Args:
            module: The calling module name
            filename: Logical filename identifier

        Returns:
            Age in minutes as a float, or None if the entry does not exist
        """
        return await self.backend.age(module, filename)

    async def fetch_cached(self, module, filename, fetch_fn, fresh_minutes=30, stale_minutes=35, format="yaml"):
        """Fetch-or-cache via the storage backend's stale-while-revalidate helper.

        Args:
            module: The calling module name
            filename: Logical filename identifier
            fetch_fn: Zero-arg coroutine function that fetches fresh data
            fresh_minutes: Below this age the cached value is returned without refresh
            stale_minutes: Between fresh_minutes and this, serve stale while one caller refreshes
            format: One of 'yaml', 'json', or 'text'

        Returns:
            Cached or freshly fetched data, or None if nothing could be obtained
        """
        return await self.backend.fetch_cached(module, filename, fetch_fn, fresh_minutes=fresh_minutes, stale_minutes=stale_minutes, format=format)

    async def run(self, seconds, first):
        """Run the storage component, cleaning up expired files every hour.

        Args:
            seconds: Elapsed seconds since component started
            first: True on first call

        Returns:
            True always
        """
        if seconds % 3600 == 0:
            await self.backend.cleanup()
        self.update_success_timestamp()
        return True
