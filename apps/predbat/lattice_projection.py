# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice projection table + glue
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Curated projection of Lattice capabilities onto existing predbat.* entities.

Only the capabilities listed here are routed through the Lattice resolver; every
other entity behaves exactly as today (incremental, reversible adoption).
"""
from dataclasses import dataclass

from lattice import merge_fragments, resolve_control, resolve_read, control_candidates, Fragment, ResolveResult


@dataclass(frozen=True)
class ProjectionEntry:
    """One curated mapping: (capability, scope) to a predbat.* entity plus direction."""

    capability: str
    scope: str
    entity: str
    read: bool
    write: bool


# Curated capabilities routed through the resolver. Charge/discharge rate is the first slice
# (largest provider-fallback win); target/reserve SOC follow. Each is per-device (battery-system)
# scope so it resolves to a single node's access path. Plant-scope reads (e.g. aggregate soc)
# need plant aggregation in the resolver — deferred.
PROJECTION_TABLE = (
    ProjectionEntry("charge_rate", "battery-system", "predbat.charge_rate", read=True, write=True),
    ProjectionEntry("discharge_rate", "battery-system", "predbat.discharge_rate", read=True, write=True),
    ProjectionEntry("target_soc", "battery-system", "predbat.target_soc", read=True, write=True),
    ProjectionEntry("reserve_soc", "battery-system", "predbat.reserve_soc", read=True, write=True),
)


def projection_entries():
    """Return all curated projection entries."""
    return PROJECTION_TABLE


def entity_for(capability, scope):
    """Return the ProjectionEntry for (capability, scope), or None if not projected."""
    for entry in PROJECTION_TABLE:
        if entry.capability == capability and entry.scope == scope:
            return entry
    return None


class LatticeProjection:
    """Collects producer fragments, merges them, and routes projected reads/writes.

    Feature-flagged: callers only consult it for capabilities in PROJECTION_TABLE
    and only when enabled; everything else stays on today's code paths.
    """

    def __init__(self, base):
        """Hold the PredBat base for component access and logging."""
        self.base = base
        self.site = None
        self.providers = {}  # provider id -> producing component (built at refresh)

    def enabled(self):
        """True when the Lattice projection is switched on (default off)."""
        return bool(self.base.get_arg("lattice_projection_enable", False))

    def _producers(self):
        """Yield (name, component) for every registered component that publishes a fragment.

        Discovery is data-driven: ANY component implementing lattice_fragment is a producer,
        so a new integration (Fox, Solax, Solis, ...) is picked up here with no change.
        """
        registry = getattr(self.base, "components", None)
        if registry is None:
            return
        for name in registry.get_all():
            comp = registry.get_component(name)
            if comp is not None and hasattr(comp, "lattice_fragment"):
                yield name, comp

    def refresh(self):
        """Re-collect fragments from all producers and rebuild the merged site graph.

        Each producer declares its own provider id in the fragment, so the provider->component
        map is built from the data — there is no hardcoded brand list.
        """
        fragments = []
        self.providers = {}
        for name, comp in self._producers():
            try:
                fragment = Fragment.from_dict(comp.lattice_fragment())
            except Exception as exc:  # a bad producer must not break the others
                self.base.log("Warn: lattice: producer {} failed: {}".format(name, exc))
                continue
            if fragment.provider:
                self.providers[fragment.provider] = comp
            fragments.append(fragment)
        self.site = merge_fragments(fragments)
        return self.site

    def live_providers(self):
        """Provider ids currently reachable (a producing component that reports alive)."""
        live = set()
        for provider, comp in self.providers.items():
            try:
                if comp.is_alive():
                    live.add(provider)
            except Exception:  # treat an unhealthy producer as unavailable
                continue
        return live

    def write(self, capability, scope, node_id, value, available=None):
        """Resolve a projected control write; returns a ResolveResult naming the chosen provider."""
        if entity_for(capability, scope) is None or self.site is None:
            return ResolveResult(reason="not projected")
        avail = available if available is not None else self.live_providers()
        return resolve_control(self.site, capability, node_id, value, avail)

    def read(self, capability, scope, node_id, available=None):
        """Resolve a projected read; returns a ResolveResult naming the chosen provider."""
        if entity_for(capability, scope) is None or self.site is None:
            return ResolveResult(reason="not projected")
        avail = available if available is not None else self.live_providers()
        return resolve_read(self.site, capability, node_id, avail)

    def _component_for_provider(self, provider):
        """Return the producing component that backs a provider id, or None."""
        return self.providers.get(provider)

    def would_handle(self, capability, scope, node_id):
        """True if enabled, the capability is projected, and a provider can currently control it.

        A cheap synchronous gate for the live write path: if this returns False the caller must
        fall back to its normal write so control is never lost.
        """
        if not self.enabled() or entity_for(capability, scope) is None or self.site is None:
            return False
        return bool(control_candidates(self.site, capability, node_id, 0, self.live_providers()))

    async def apply(self, capability, scope, node_id, value, available=None):
        """Resolve AND execute a projected control write, trying providers in preference order.

        Iterates the ranked control candidates; calls each provider's async lattice_control
        and returns on the first success. Falls back to the next provider not only when one is
        unavailable but when its write FAILS — the real gateway->cloud resilience.
        """
        if entity_for(capability, scope) is None or self.site is None:
            return ResolveResult(reason="not projected")
        avail = available if available is not None else self.live_providers()
        candidates = control_candidates(self.site, capability, node_id, value, avail)
        if not candidates:
            return ResolveResult(reason="no available control path")
        for provider, access_path, clamped in candidates:
            comp = self._component_for_provider(provider)
            if comp is None or not hasattr(comp, "lattice_control"):
                continue
            try:
                ok = await comp.lattice_control(node_id, capability, clamped)
            except Exception as exc:  # a failing provider must not stop the fallback
                self.base.log("Warn: lattice: {} control raised: {}".format(provider, exc))
                ok = False
            if ok:
                return ResolveResult(ok=True, provider=provider, access_path=access_path, value=clamped)
        return ResolveResult(reason="all providers failed")
