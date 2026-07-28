# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice fragment adapter registry
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Generic durable integration adapters for compiled Lattice fragments.

This module is deliberately additive and default-off.  Nothing discovers or
registers production integrations unless a caller explicitly enables a
``FragmentAdapterRegistry`` and asks it to create a compiler.

An integration owns one ``DurableFragmentAdapter`` and its durable state store.
The adapter atomically binds every generation to one semantic fingerprint and
one immutable ``ProviderSnapshot``.  The registry only discovers the common
``lattice_fragment_adapter()`` surface; it has no provider or brand allow-list.
"""

# cspell:ignore autoconfig idempotently unsubscribers

import hashlib
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Optional, Protocol

from lattice_autoconfig import (
    ProviderHealth,
    ProviderSnapshot,
    _fingerprint_snapshot,
    _plain,
)
from lattice_compiled_publication import CompiledLatticeCompiler


class FragmentAdapterError(RuntimeError):
    """Base error for fail-closed fragment adapter operations."""


class FragmentAdapterReadError(FragmentAdapterError):
    """A durable fragment could not be read or validated."""


class FragmentAdapterConflict(FragmentAdapterError):
    """An atomic fragment publication lost to a different durable state."""


class FragmentAdapterRemoved(FragmentAdapterReadError):
    """A registered integration has published a durable removal tombstone."""


def _validate_provider_id(provider_id):
    """Normalize one provider-owned stable identifier."""
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("provider_id must be a non-empty string")
    return provider_id.strip()


def _validate_generation(generation):
    """Validate one monotonically increasing integration generation."""
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValueError("generation must be a non-negative integer")


def _validate_reason(reason):
    """Normalize one auditable invalidation reason."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    return reason.strip()


def _semantic_fingerprint(snapshot, removed=False):
    """Return the compiler's generation-bound semantic safety fingerprint."""
    fingerprint = _fingerprint_snapshot(snapshot)
    if removed:
        return hashlib.sha256("removed:{}".format(fingerprint).encode("utf-8")).hexdigest()
    return fingerprint


@dataclass(frozen=True)
class FragmentAdapterState:
    """One integration-owned durable fragment cursor and immutable value.

    ``semantic_fingerprint`` intentionally uses the compiler's exact
    generation-bound fingerprint.  The pair therefore detects both generation
    regression and reuse of one generation for different safety-relevant
    content.
    """

    provider_id: str
    generation: int
    semantic_fingerprint: str
    snapshot: ProviderSnapshot
    removed: bool = False

    def __post_init__(self):
        """Validate that the durable cursor exactly binds its snapshot."""
        provider_id = _validate_provider_id(self.provider_id)
        _validate_generation(self.generation)
        if not isinstance(self.snapshot, ProviderSnapshot):
            raise ValueError("snapshot must be ProviderSnapshot")
        if self.snapshot.provider_id != provider_id:
            raise ValueError("snapshot provider_id does not match durable state")
        if self.snapshot.generation != self.generation:
            raise ValueError("snapshot generation does not match durable state")
        if not isinstance(self.removed, bool):
            raise ValueError("removed must be a boolean")
        expected = _semantic_fingerprint(self.snapshot, self.removed)
        if self.semantic_fingerprint != expected:
            raise ValueError("semantic_fingerprint does not match the immutable snapshot")
        object.__setattr__(self, "provider_id", provider_id)


class FragmentAdapterStateStore:
    """Required atomic durable-store protocol owned by one integration."""

    def load(self):
        """Return the current ``FragmentAdapterState`` or ``None``."""
        raise NotImplementedError

    def compare_and_store(self, expected, replacement):
        """Atomically store replacement only when current state equals expected."""
        raise NotImplementedError


class FragmentPublisher(Protocol):
    """Structural integration protocol discovered without provider knowledge."""

    provider_id: str

    def read_state(self) -> FragmentAdapterState:
        """Fresh-read the integration-owned durable state."""
        ...

    def read_snapshot(self) -> ProviderSnapshot:
        """Fresh-read the current immutable provider snapshot."""
        ...

    def subscribe_invalidation(self, listener):
        """Attach the registry's invalidation sink and return an unsubscribe."""
        ...


class FragmentPublishingComponent(Protocol):
    """Structural component discovery surface used by the generic registry."""

    def lattice_fragment_adapter(self) -> Optional[FragmentPublisher]:
        """Return this component's fragment publisher, if it has one."""
        ...


class InMemoryFragmentAdapterStateStore(FragmentAdapterStateStore):
    """Thread-safe reference state store for tests; not production durability."""

    def __init__(self, state=None):
        """Create a store optionally seeded with one validated state."""
        if state is not None and not isinstance(state, FragmentAdapterState):
            raise ValueError("initial state must be FragmentAdapterState or None")
        self._lock = threading.RLock()
        self._state = state
        self._writes = 0

    @property
    def writes(self):
        """Return the number of successful atomic writes."""
        with self._lock:
            return self._writes

    def load(self):
        """Return the immutable current state."""
        with self._lock:
            return self._state

    def compare_and_store(self, expected, replacement):
        """Atomically compare the complete cursor and install replacement."""
        if replacement is not None and not isinstance(
            replacement,
            FragmentAdapterState,
        ):
            raise ValueError("replacement must be FragmentAdapterState or None")
        with self._lock:
            if self._state != expected:
                return False
            self._state = replacement
            self._writes += 1
            return True


class DurableFragmentAdapter:
    """Generic publisher over one integration-owned atomic state store."""

    def __init__(self, provider_id, state_store):
        """Restore one provider cursor without performing discovery or writes."""
        self.provider_id = _validate_provider_id(provider_id)
        if not callable(getattr(state_store, "load", None)) or not callable(getattr(state_store, "compare_and_store", None)):
            raise ValueError("state_store must provide load and compare_and_store")
        self._state_store = state_store
        self._lock = threading.RLock()
        self._listeners = []
        self._validate_loaded_state(self._load())

    def _load(self):
        """Load durable state and convert store faults into adapter faults."""
        try:
            return self._state_store.load()
        except Exception as exc:
            raise FragmentAdapterReadError(
                "durable fragment load failed: {}: {}".format(
                    type(exc).__name__,
                    exc,
                )
            ) from exc

    def _validate_loaded_state(self, state):
        """Validate one store result and its provider ownership."""
        if state is None:
            return None
        if not isinstance(state, FragmentAdapterState):
            raise FragmentAdapterReadError("state_store.load must return FragmentAdapterState or None")
        try:
            state.__post_init__()
        except ValueError as exc:
            raise FragmentAdapterReadError(str(exc)) from exc
        if state.provider_id != self.provider_id:
            raise FragmentAdapterReadError("durable state belongs to provider {}".format(state.provider_id))
        return state

    def read_state(self):
        """Fresh-read the complete immutable durable fragment state."""
        with self._lock:
            state = self._validate_loaded_state(self._load())
            if state is None:
                raise FragmentAdapterReadError("provider {} has no durable fragment".format(self.provider_id))
            return state

    def read_snapshot(self):
        """Fresh-read one immutable snapshot or fail closed on removal."""
        state = self.read_state()
        if state.removed:
            raise FragmentAdapterRemoved(
                "provider {} was removed at generation {}".format(
                    self.provider_id,
                    state.generation,
                )
            )
        return state.snapshot

    def subscribe_invalidation(self, listener):
        """Subscribe the compiler-facing invalidation sink."""
        if not callable(listener):
            raise ValueError("invalidation listener must be callable")
        with self._lock:
            if listener in self._listeners:
                raise ValueError("invalidation listener is already subscribed")
            self._listeners.append(listener)
        closed = [False]

        def unsubscribe():
            """Detach this exact listener idempotently."""
            with self._lock:
                if closed[0]:
                    return
                closed[0] = True
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def publish(
        self,
        snapshot,
        reason,
        feedback_token=None,
        removed=False,
    ):
        """Atomically publish a newer fragment and notify every subscriber.

        Invalidation is announced before the durable CAS.  Consequently a
        failed or conflicting store write leaves the compiler pending and its
        fresh reader behind the requested generation, which fails closed.
        A subscriber may return ``False`` only to suppress publication-origin
        feedback; that token then causes neither persistence nor recompilation.
        """
        if not isinstance(snapshot, ProviderSnapshot):
            raise ValueError("snapshot must be ProviderSnapshot")
        if snapshot.provider_id != self.provider_id:
            raise ValueError("snapshot provider_id does not match adapter")
        if not isinstance(removed, bool):
            raise ValueError("removed must be a boolean")
        reason = _validate_reason(reason)
        candidate = FragmentAdapterState(
            self.provider_id,
            snapshot.generation,
            _semantic_fingerprint(snapshot, removed),
            snapshot,
            removed,
        )

        with self._lock:
            current = self._validate_loaded_state(self._load())
            if current is not None:
                if candidate.generation < current.generation:
                    raise ValueError(
                        "fragment generation {} regressed from {}".format(
                            candidate.generation,
                            current.generation,
                        )
                    )
                if candidate.generation == current.generation:
                    if candidate.semantic_fingerprint != (current.semantic_fingerprint):
                        raise ValueError("fragment generation {} was reused with different " "content".format(candidate.generation))
                    return False
            listeners = tuple(self._listeners)

            for listener in listeners:
                accepted = listener(
                    self.provider_id,
                    candidate.generation,
                    reason,
                    feedback_token,
                )
                if accepted is False:
                    return False

            try:
                committed = self._state_store.compare_and_store(
                    current,
                    candidate,
                )
            except Exception as exc:
                raise FragmentAdapterConflict(
                    "durable fragment publication failed: {}: {}".format(
                        type(exc).__name__,
                        exc,
                    )
                ) from exc
            if committed is not True:
                winner = self._validate_loaded_state(self._load())
                if winner == candidate:
                    return False
                raise FragmentAdapterConflict("durable fragment cursor changed before atomic publication")
            return True

    def remove(self, generation, reason, feedback_token=None):
        """Publish a durable removal tombstone and invalidate the compiler."""
        _validate_generation(generation)
        current = self.read_state()
        snapshot = current.snapshot
        tombstone = ProviderSnapshot(
            self.provider_id,
            generation,
            ProviderHealth.OFFLINE,
            _plain(snapshot.topology_fragment),
            snapshot.aliases,
            snapshot.identity_aliases,
            snapshot.role_assignments,
            snapshot.config_projections,
        )
        return self.publish(
            tombstone,
            reason,
            feedback_token=feedback_token,
            removed=True,
        )


class FragmentAdapterRegistry:
    """Default-off brand-neutral discovery and frozen compiler registry."""

    DISCOVERY_METHOD = "lattice_fragment_adapter"

    def __init__(self, enabled=False):
        """Create an empty registry; disabled is the safe default."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        self._enabled = enabled
        self._lock = threading.RLock()
        self._adapters = {}
        self._compiler = None
        self._unsubscribers = ()
        self._sealed = False

    @property
    def enabled(self):
        """Return whether explicit fragment discovery is enabled."""
        return self._enabled

    @property
    def provider_ids(self):
        """Return registered provider identities in deterministic order."""
        with self._lock:
            return tuple(sorted(self._adapters))

    @property
    def readers(self):
        """Return an immutable provider-reader mapping for inspection/tests."""
        with self._lock:
            return MappingProxyType({provider_id: adapter.read_snapshot for provider_id, adapter in self._adapters.items()})

    def _validate_adapter(self, adapter):
        """Validate the common adapter surface and its durable current state."""
        provider_id = _validate_provider_id(getattr(adapter, "provider_id", None))
        for method_name in (
            "read_state",
            "read_snapshot",
            "subscribe_invalidation",
        ):
            if not callable(getattr(adapter, method_name, None)):
                raise ValueError("fragment adapter must provide {}".format(method_name))
        state = adapter.read_state()
        if not isinstance(state, FragmentAdapterState):
            raise ValueError("adapter read_state must return FragmentAdapterState")
        if state.provider_id != provider_id:
            raise ValueError("adapter state belongs to provider {}".format(state.provider_id))
        return provider_id

    def discover(self, components):
        """Discover every component implementing the common publisher surface."""
        if not self._enabled:
            return ()
        candidates = []
        for component in tuple(components):
            factory = getattr(component, self.DISCOVERY_METHOD, None)
            if factory is None:
                continue
            if not callable(factory):
                raise ValueError("{} must be callable".format(self.DISCOVERY_METHOD))
            adapter = factory()
            if adapter is not None:
                candidates.append(adapter)

        validated = []
        seen = set()
        for adapter in candidates:
            provider_id = self._validate_adapter(adapter)
            if provider_id in seen:
                raise ValueError("provider {} was discovered more than once".format(provider_id))
            seen.add(provider_id)
            validated.append((provider_id, adapter))

        with self._lock:
            if self._sealed:
                raise RuntimeError("fragment registry membership is sealed")
            duplicate = sorted(provider_id for provider_id, _adapter in validated if provider_id in self._adapters)
            if duplicate:
                raise ValueError("provider {} is already registered".format(duplicate[0]))
            for provider_id, adapter in validated:
                self._adapters[provider_id] = adapter
            return tuple(provider_id for provider_id, _adapter in validated)

    def register(self, adapter):
        """Register one explicitly supplied generic adapter before sealing."""
        if not self._enabled:
            return False
        provider_id = self._validate_adapter(adapter)
        with self._lock:
            if self._sealed:
                raise RuntimeError("fragment registry membership is sealed")
            if provider_id in self._adapters:
                raise ValueError("provider {} is already registered".format(provider_id))
            self._adapters[provider_id] = adapter
            return True

    def unregister(self, provider_id):
        """Remove only pre-bind registration; runtime removal is a tombstone."""
        if not self._enabled:
            return False
        provider_id = _validate_provider_id(provider_id)
        with self._lock:
            if self._sealed:
                raise RuntimeError("runtime unregister is unsafe; publish a durable removal " "tombstone")
            if provider_id not in self._adapters:
                raise KeyError("unknown provider {}".format(provider_id))
            del self._adapters[provider_id]
            return True

    def create_compiler(self, state_store, override_reader=None):
        """Freeze membership and create the sole compiled-Lattice coordinator."""
        if not self._enabled:
            raise RuntimeError("fragment adapter registry is disabled")
        with self._lock:
            if self._sealed:
                raise RuntimeError("fragment registry membership is already sealed")
            if not self._adapters:
                raise RuntimeError("cannot create a compiler without fragment adapters")
            for adapter in self._adapters.values():
                self._validate_adapter(adapter)
            readers = {provider_id: adapter.read_snapshot for provider_id, adapter in self._adapters.items()}
            compiler = CompiledLatticeCompiler(
                readers,
                state_store=state_store,
                override_reader=override_reader,
            )

            unsubscribers = []
            try:
                for provider_id, adapter in sorted(self._adapters.items()):

                    def invalidate(
                        source_id,
                        generation,
                        reason,
                        feedback_token,
                        expected_id=provider_id,
                    ):
                        """Forward this adapter's invalidation to the compiler."""
                        if source_id != expected_id:
                            raise FragmentAdapterError(
                                "adapter {} emitted invalidation for {}".format(
                                    expected_id,
                                    source_id,
                                )
                            )
                        accepted = compiler.invalidate(
                            source_id,
                            generation,
                            reason,
                            feedback_token,
                        )
                        if feedback_token is not None and accepted is False:
                            return False
                        return True

                    unsubscribe = adapter.subscribe_invalidation(invalidate)
                    if not callable(unsubscribe):
                        raise ValueError("subscribe_invalidation must return an " "unsubscribe callable")
                    unsubscribers.append(unsubscribe)
            except Exception:
                for unsubscribe in reversed(unsubscribers):
                    unsubscribe()
                raise

            self._compiler = compiler
            self._unsubscribers = tuple(unsubscribers)
            self._sealed = True
            return compiler
