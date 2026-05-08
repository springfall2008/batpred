"""Storage backend abstraction for component cache I/O.

Provides a thin interface over file I/O so components can be tested with an
in-memory backend and deployed in SaaS with a KeyDB backend, without touching
cache logic (age calculation, JSON parsing, stale fallback).

The SaaS-only KeyDB implementation lives in predbat-saas-images.
"""

import os
from typing import Optional


class StorageBackend:
    """Thin storage backend interface — raw bytes in, raw bytes out.

    Implementations provide the I/O layer only. JSON parsing, age calculation,
    TTL policy, and stale-fallback logic belong in the component that uses the
    backend, not here.
    """

    async def read(self, key: str) -> Optional[bytes]:
        """Read stored bytes for key. Returns None if key does not exist."""
        raise NotImplementedError

    async def write(self, key: str, data: bytes, ttl_seconds: Optional[int] = None) -> None:
        """Write bytes for key. ttl_seconds is advisory; filesystem implementations ignore it."""
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        """Delete a stored entry. No-op if key does not exist."""
        raise NotImplementedError


class FilesystemStorageBackend(StorageBackend):
    """Filesystem-backed storage. Each key maps to a file under cache_path."""

    def __init__(self, cache_path: str) -> None:
        """Initialise with the directory used to store cache files."""
        self._cache_path = cache_path

    async def read(self, key: str) -> Optional[bytes]:
        """Read a cache file. Returns None if the file does not exist."""
        path = os.path.join(self._cache_path, key)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    async def write(self, key: str, data: bytes, ttl_seconds: Optional[int] = None) -> None:
        """Write bytes to a cache file. ttl_seconds is ignored for filesystem storage."""
        os.makedirs(self._cache_path, exist_ok=True)
        with open(os.path.join(self._cache_path, key), "wb") as f:
            f.write(data)

    async def delete(self, key: str) -> None:
        """Delete a cache file. No-op if the file does not exist."""
        try:
            os.remove(os.path.join(self._cache_path, key))
        except OSError:
            pass


class MemoryStorageBackend(StorageBackend):
    """In-memory storage backend for tests."""

    def __init__(self) -> None:
        """Initialise with an empty store."""
        self._store: dict = {}

    async def read(self, key: str) -> Optional[bytes]:
        """Read from the in-memory store."""
        return self._store.get(key)

    async def write(self, key: str, data: bytes, ttl_seconds: Optional[int] = None) -> None:
        """Write to the in-memory store. ttl_seconds is ignored."""
        self._store[key] = data

    async def delete(self, key: str) -> None:
        """Delete from the in-memory store. No-op if key does not exist."""
        self._store.pop(key, None)
