# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice device-map component (read-only)
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Builds the merged Lattice device map inside the live PredBat system.

When `lattice_projection_enable` is on, this rebuilds the merged site map from every producer
component each cycle and logs it — which devices are on the network, their access paths, and the
sensors each exposes. READ-ONLY observability; it does not control anything. No-op when off.
"""
from component_base import ComponentBase
from lattice_projection import LatticeProjection


class LatticeComponent(ComponentBase):
    """Live host for the read-only Lattice device map."""

    def initialize(self, **kwargs):
        """Create the projection over the PredBat base."""
        self.projection = LatticeProjection(self.base)
        self.run_timeout = 60

    async def run(self, seconds, first):
        """Rebuild + log the merged device map when enabled; no-op when disabled."""
        if not self.projection.enabled():
            return True
        site = self.projection.refresh()
        self.log("Lattice: merged device map — {} device(s)".format(len(site.nodes)))
        for node in site.nodes:
            providers = [ap.provider for ap in node.access_paths]
            sensors = [s.capability for s in node.sensors]
            self.log("Lattice: device {} ({}) via {} — sensors {}".format(node.id, node.device_type, providers, sensors))
        return True
