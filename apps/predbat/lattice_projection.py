# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice device-map projection (read-only)
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Collects device fragments from the integration components and merges them into one site map.

READ-ONLY: this maps the devices on the network and inventories their sensors. It does not control
anything — control is a separate, deferred model (a common intent/shape/binding API; see lattice-spec).
"""
from lattice import merge_fragments, resolve_sensor, Fragment


class LatticeProjection:
    """Discovers producer components, merges their fragments, and exposes the merged device map."""

    def __init__(self, base):
        """Hold the PredBat base for component access and logging."""
        self.base = base
        self.site = None

    def enabled(self):
        """True when device mapping is switched on (default off)."""
        return bool(self.base.get_arg("lattice_projection_enable", False))

    def _producers(self):
        """Yield (name, component) for every registered component that publishes a fragment.

        Discovery is data-driven: ANY component implementing lattice_fragment is a producer, so a
        new integration is mapped here with no change.
        """
        registry = getattr(self.base, "components", None)
        if registry is None:
            return
        for name in registry.get_all():
            comp = registry.get_component(name)
            if comp is not None and hasattr(comp, "lattice_fragment"):
                yield name, comp

    def refresh(self):
        """Re-collect fragments from all producers and rebuild the merged site map."""
        fragments = []
        for name, comp in self._producers():
            try:
                fragments.append(Fragment.from_dict(comp.lattice_fragment()))
            except Exception as exc:  # a bad producer must not break the others
                self.base.log("Warn: lattice: producer {} failed: {}".format(name, exc))
        self.site = merge_fragments(fragments)
        return self.site

    def live_providers(self):
        """Provider ids whose producing component currently reports alive."""
        live = set()
        for _name, comp in self._producers():
            try:
                if comp.is_alive():
                    for node in self.site.nodes if self.site else []:
                        for ap in node.access_paths:
                            live.add(ap.provider)
                    break
            except Exception:
                continue
        return live

    def sensor_entity(self, capability, node_id):
        """Return the preferred entity for a device's sensor (best access path), or None."""
        if self.site is None:
            return None
        return resolve_sensor(self.site, capability, node_id)
