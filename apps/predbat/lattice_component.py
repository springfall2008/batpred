# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice projection component
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Runs the Lattice projection inside the live PredBat system.

When `lattice_projection_enable` is on, this component periodically rebuilds the merged site
graph from every producer component and logs what it sees — which devices, their ranked access
paths, and which providers are currently reachable. This is observability only: it does NOT
route or execute control (the inverter hot-path wiring is a separate, reviewed step). When the
flag is off the component is a no-op, so it is safe to register unconditionally.
"""
from component_base import ComponentBase
from lattice_projection import LatticeProjection


class LatticeComponent(ComponentBase):
    """Live host for the Lattice projection (shadow/observability for now)."""

    def initialize(self, **kwargs):
        """Create the projection over the PredBat base."""
        self.projection = LatticeProjection(self.base)
        self.run_timeout = 60

    async def run(self, seconds, first):
        """Rebuild + log the merged site graph when enabled; no-op when disabled."""
        if not self.projection.enabled():
            return True
        site = self.projection.refresh()
        live = self.projection.live_providers()
        self.log("Lattice: merged site graph has {} node(s); live providers: {}".format(len(site.nodes), sorted(live)))
        for node in site.nodes:
            providers = [ap.provider for ap in node.access_paths]
            reachable = [p for p in providers if p in live]
            self.log("Lattice: node {} ({}) access paths {} -> reachable {}".format(node.id, node.device_type, providers, reachable))
        return True
