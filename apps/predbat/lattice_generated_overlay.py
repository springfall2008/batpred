# -----------------------------------------------------------------------------
# Predbat Home Battery System - generated Lattice configuration overlay
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Atomic, immutable storage for generated Lattice configuration arguments."""

import copy
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


def _freeze(value):
    """Detach and recursively freeze a generated configuration value."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _thaw(value):
    """Return a detached mutable value suitable for Predbat's argument readers."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return set(copy.deepcopy(value))
    return copy.deepcopy(value)


@dataclass(frozen=True)
class GeneratedConfigSnapshot:
    """One immutable full-replacement generated configuration snapshot."""

    enabled: bool
    version: int
    arguments: Mapping


class GeneratedConfigOverlay:
    """Publish and read complete generated argument snapshots atomically.

    Readers capture one immutable snapshot reference without locking. Writers are
    serialized and replace that reference only after the complete next snapshot
    has been detached and frozen. Values are thawed on every successful read so
    existing argument-resolution code cannot mutate the published snapshot.
    """

    def __init__(self, authoritative_arguments=()):
        """Create a disabled overlay with no generated arguments."""
        if any(not isinstance(argument, str) or not argument.strip() for argument in authoritative_arguments):
            raise ValueError("authoritative argument names must be non-empty strings")
        self._write_lock = threading.Lock()
        self._authoritative_arguments = frozenset(authoritative_arguments)
        self._snapshot = GeneratedConfigSnapshot(
            enabled=False,
            version=0,
            arguments=MappingProxyType({}),
        )

    @property
    def snapshot(self):
        """Return the current immutable snapshot for diagnostics."""
        return self._snapshot

    def replace(self, arguments):
        """Atomically enable and replace the complete generated argument set."""
        if not isinstance(arguments, Mapping):
            raise TypeError("generated arguments must be a mapping")
        if any(not isinstance(argument, str) or not argument.strip() for argument in arguments):
            raise ValueError("generated argument names must be non-empty strings")
        frozen = _freeze(dict(arguments))
        with self._write_lock:
            current = self._snapshot
            self._snapshot = GeneratedConfigSnapshot(
                enabled=True,
                version=current.version + 1,
                arguments=frozen,
            )
            return self._snapshot

    def disable(self):
        """Atomically disable and remove every generated argument."""
        with self._write_lock:
            current = self._snapshot
            self._snapshot = GeneratedConfigSnapshot(
                enabled=False,
                version=current.version + 1,
                arguments=MappingProxyType({}),
            )
            return self._snapshot

    def read(self, argument):
        """Return ``(present, detached_value)`` from one captured snapshot."""
        snapshot = self._snapshot
        if not snapshot.enabled or argument not in snapshot.arguments:
            return False, None
        return True, _thaw(snapshot.arguments[argument])

    def authoritative(self, argument):
        """Return whether a published value is a topology-owned hardware fact."""
        snapshot = self._snapshot
        return snapshot.enabled and argument in snapshot.arguments and argument in self._authoritative_arguments
