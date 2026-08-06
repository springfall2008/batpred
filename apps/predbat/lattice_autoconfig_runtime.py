# cspell:ignore XIAO
# -----------------------------------------------------------------------------
# Predbat Home Battery System - live Lattice automatic configuration runtime
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Default-off runtime coordinator for durable generated configuration."""

# cspell:ignore autoconfig

import threading
from dataclasses import dataclass

from gateway_autoconfig import compile_gateway_auto_config
from lattice_autoconfig import CompileStatus, ProviderHealth, _plain
from lattice_durable_storage import (
    PredBatCompiledLatticeStateStore,
    PredBatFragmentAdapterStateStore,
)
from lattice_fragment_adapters import (
    FragmentAdapterConflict,
    FragmentAdapterReadError,
    FragmentAdapterRegistry,
)
from lattice_gateway_fragment import GatewayRetainedTopologyFragmentPublisher
from lattice_ge_cloud_fragment import (
    GECloudDeviceSnapshot,
    GECloudFragmentPublisher,
)
from lattice_generated_overlay import GeneratedConfigOverlay
from lattice_provider_inputs import (
    gateway_status_to_lattice_input,
    gecloud_state_to_lattice_input,
)


@dataclass(frozen=True)
class LatticeRuntimeResult:
    """Observable result of one provider-triggered reconciliation."""

    accepted: bool
    staged: bool
    digest: object
    status: object
    issues: tuple


class LatticeAutoConfigRuntime:
    """Own durable publishers, compilation, and one generated config overlay."""

    GATEWAY_PROVIDER_ID = "predbat-gateway"
    GECLOUD_PROVIDER_ID = "ge-cloud"

    def __init__(self, base, storage, enabled=False):
        """Create an unwired runtime; disabled construction has no durable writes."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        self.base = base
        self.log = base.log
        self.enabled = enabled
        self.overlay = GeneratedConfigOverlay(
            authoritative_arguments=("inverter_hybrid",),
        )
        self.base.lattice_generated_overlay = self.overlay
        self._lock = threading.RLock()
        self._registry = None
        self._compiler = None
        self._gateway = None
        self._gecloud = None
        self._compiled_store = None
        self._pending = None
        self._pending_digest = None
        self._pending_disable = False
        self._applied_digest = None
        self._provider_ids = frozenset()
        self._fresh_providers = set()
        if not enabled:
            return
        if storage is None:
            raise ValueError("enabled Lattice auto-config requires storage")

        self._gateway = GatewayRetainedTopologyFragmentPublisher(
            self.GATEWAY_PROVIDER_ID,
            PredBatFragmentAdapterStateStore(storage, self.GATEWAY_PROVIDER_ID),
            enabled=True,
        )
        self._gecloud = GECloudFragmentPublisher(
            self.GECLOUD_PROVIDER_ID,
            PredBatFragmentAdapterStateStore(storage, self.GECLOUD_PROVIDER_ID),
            enabled=True,
        )
        self._compiled_store = PredBatCompiledLatticeStateStore(storage)
        self._seed_or_revalidate_publishers()

    @property
    def gateway_publisher(self):
        """Return the long-lived Gateway publisher shared across restarts."""
        return self._gateway if self.enabled else None

    @property
    def gecloud_publisher(self):
        """Return the long-lived GE Cloud publisher shared across restarts."""
        return self._gecloud if self.enabled else None

    @property
    def compiler(self):
        """Return the bound compiler for diagnostics."""
        return self._compiler

    def _seed_or_revalidate_publishers(self):
        """Ensure registry-visible offline state without trusting restart health."""
        empty_gateway = {
            "topologyVersion": "0.3.0",
            "scope": "fragment",
            "docVersion": 0,
            "producer": {
                "name": "PredBat Gateway",
                "provider": self.GATEWAY_PROVIDER_ID,
                "authority": 10,
            },
            "nodes": [],
        }
        try:
            gateway_state = self._gateway.read_state()
        except FragmentAdapterReadError:
            self._gateway.ingest_retained_topology(empty_gateway, online=False)
        else:
            if gateway_state.snapshot.health is not ProviderHealth.OFFLINE:
                self._gateway.set_liveness(False)

        try:
            ge_state = self._gecloud.read_state()
        except FragmentAdapterReadError:
            self._gecloud.ingest_discovery(
                0,
                (
                    GECloudDeviceSnapshot(
                        serial="LATTICE-PENDING",
                        kind="inverter",
                        online=False,
                    ),
                ),
                health=False,
            )
        else:
            if ge_state.snapshot.health is not ProviderHealth.OFFLINE:
                self._gecloud.set_liveness(False)

    def bind(self, gateway_enabled=False, gecloud_enabled=False):
        """Freeze the active provider set before phase-one components start."""
        if not self.enabled:
            return ()
        if not isinstance(gateway_enabled, bool) or not isinstance(
            gecloud_enabled,
            bool,
        ):
            raise ValueError("provider activation flags must be booleans")
        with self._lock:
            if self._compiler is not None:
                raise RuntimeError("Lattice auto-config runtime is already bound")
            registry = FragmentAdapterRegistry(enabled=True)
            provider_ids = []
            if gateway_enabled:
                registry.register(self._gateway)
                provider_ids.append(self.GATEWAY_PROVIDER_ID)
            if gecloud_enabled:
                registry.register(self._gecloud)
                provider_ids.append(self.GECLOUD_PROVIDER_ID)
            if not provider_ids:
                self._registry = registry
                return ()
            self._compiler = registry.create_compiler(
                self._compiled_store,
                atomic_materializer=True,
                allow_provider_failover=True,
            )
            self._registry = registry
            self._provider_ids = frozenset(provider_ids)
            return tuple(provider_ids)

    def provider_active(self, provider_id):
        """Return whether one provider belongs to this frozen runtime binding."""
        return self.enabled and provider_id in self._provider_ids

    def _invalidate_pending(self):
        """Cancel stale queued config and stage removal of an active overlay."""
        changed = self._pending is not None
        self._pending = None
        self._pending_digest = None
        if self.overlay.snapshot.enabled:
            changed = changed or not self._pending_disable
            self._pending_disable = True
        else:
            self._pending_disable = False
        if changed:
            self.base.update_pending = True
            self.base.plan_valid = False
        return changed

    def _stage_publication(self, publication):
        """Queue one complete immutable config replacement by digest."""
        if publication is None:
            return False
        plan = publication.plan
        if not plan.materialization_readiness.ready:
            return False
        if plan.digest == self._applied_digest:
            self._pending = None
            self._pending_digest = None
            self._pending_disable = False
            return False
        if plan.digest == self._pending_digest and not self._pending_disable:
            return False
        self._pending = _plain(plan.projected_config)
        self._pending_digest = plan.digest
        self._pending_disable = False
        self.base.update_pending = True
        self.base.plan_valid = False
        return True

    def reconcile(self):
        """Drain invalidations and queue only a fresh durable publication."""
        if not self.enabled or self._compiler is None:
            return LatticeRuntimeResult(False, False, None, None, ())
        with self._lock:
            run = self._compiler.drain()
            publication = run.publication
            accepted = publication is not None and run.status in (CompileStatus.FRESH, CompileStatus.DEGRADED) and publication.plan.materialization_readiness.ready
            staged = self._stage_publication(publication) if accepted else self._invalidate_pending()
            return LatticeRuntimeResult(
                accepted,
                staged,
                publication.digest if publication is not None else None,
                run.status,
                tuple(run.issues),
            )

    def apply_pending(self):
        """Atomically apply one queued full replacement at a config boundary."""
        if not self.enabled:
            return False
        with self._lock:
            if self._pending_disable:
                self.overlay.disable()
                self._pending_disable = False
                self._pending = None
                self._pending_digest = None
                self._applied_digest = None
                self.base.inverters = []
                self.base.plan_valid = False
                return True
            if self._pending is None:
                return False
            pending = self._pending
            digest = self._pending_digest
            self.overlay.replace(pending)
            self._pending = None
            self._pending_digest = None
            self._pending_disable = False
            self._applied_digest = digest
            self.base.inverters = []
            self.base.plan_valid = False
            return True

    def disable(self):
        """Remove all generated values without mutating explicit arguments."""
        with self._lock:
            changed = self.overlay.snapshot.enabled or self._pending is not None or self._pending_disable
            self._pending = None
            self._pending_digest = None
            self._pending_disable = False
            self._applied_digest = None
            self.overlay.disable()
            if changed:
                self.base.inverters = []
                self.base.update_pending = True
                self.base.plan_valid = False
            return changed

    def ingest_gateway_status(self, status, prefix="predbat", serial_filter=()):
        """Publish one complete XIAO/Gateway telemetry-derived fragment."""
        if not self.enabled or self._compiler is None:
            return None, LatticeRuntimeResult(False, False, None, None, ())
        live = gateway_status_to_lattice_input(
            status,
            provider_id=self.GATEWAY_PROVIDER_ID,
        )
        current = self._gateway.read_state()
        previous = _plain(current.snapshot.topology_fragment)
        previous_version = int(previous.get("docVersion", 0))
        candidate = dict(live.topology_fragment)
        previous_without_version = dict(previous)
        previous_without_version.pop("docVersion", None)
        document_version = previous_version if candidate == previous_without_version else previous_version + 1
        candidate["docVersion"] = document_version
        plan = compile_gateway_auto_config(
            live.inverters,
            prefix=prefix,
            serial_filter=serial_filter,
        )
        self._gateway.ingest_complete(candidate, plan, online=True)
        self._fresh_providers.add(self.GATEWAY_PROVIDER_ID)
        return plan, self.reconcile()

    def set_gateway_liveness(self, online):
        """Publish Gateway liveness and reconcile local/cloud selection."""
        if not self.enabled or self._compiler is None:
            return LatticeRuntimeResult(False, False, None, None, ())
        if not online:
            with self._lock:
                self._fresh_providers.discard(self.GATEWAY_PROVIDER_ID)
                # Cancel the in-memory candidate before the durable write. If
                # persistence fails, a rejected telemetry batch still cannot be
                # activated at the next configuration boundary.
                self._invalidate_pending()
        if online and self.GATEWAY_PROVIDER_ID not in self._fresh_providers:
            return self.reconcile()
        self._gateway.set_liveness(online)
        return self.reconcile()

    def ingest_gecloud_state(
        self,
        devices,
        settings,
        info,
        prefix="predbat",
        **flags,
    ):
        """Publish one complete GE discovery/config state and reconcile."""
        if not self.enabled or self._compiler is None:
            return None, LatticeRuntimeResult(False, False, None, None, ())
        live = gecloud_state_to_lattice_input(
            devices,
            settings,
            info,
            prefix=prefix,
            **flags,
        )
        version = self._gecloud.discovery_version
        try:
            self._gecloud.ingest_complete(
                version,
                live.devices,
                live.auto_config,
                health=True,
            )
        except FragmentAdapterConflict:
            self._gecloud.ingest_complete(
                version + 1,
                live.devices,
                live.auto_config,
                health=True,
            )
        self._fresh_providers.add(self.GECLOUD_PROVIDER_ID)
        return live, self.reconcile()

    def set_gecloud_liveness(self, health):
        """Publish GE Cloud health and reconcile local/cloud selection."""
        if not self.enabled or self._compiler is None:
            return LatticeRuntimeResult(False, False, None, None, ())
        if health is False:
            with self._lock:
                self._fresh_providers.discard(self.GECLOUD_PROVIDER_ID)
                self._invalidate_pending()
        if health is not False and self.GECLOUD_PROVIDER_ID not in self._fresh_providers:
            return self.reconcile()
        self._gecloud.set_liveness(health)
        return self.reconcile()
