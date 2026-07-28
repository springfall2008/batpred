# -----------------------------------------------------------------------------
# Predbat Home Battery System - Enphase complete-inventory Lattice adapter
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure Enphase site/device inventory normalization.

The live Enphase component is intentionally not imported.  A future gated seam
may pass a complete discovery view here only after the Enphase API has returned
explicit site and hardware evidence.  This module publishes no schedules,
settings, endpoints, credentials, features, configuration, or control claims.

Site and device IDs remain provider-local.  Only ``api_verified_serial`` is
allowed to become a strong cross-provider hardware identity.  Device kinds are
limited to the inventory classes Enphase discovery may explicitly identify:
gateway, inverter, battery, and meter.
"""

from dataclasses import dataclass
from typing import Optional

from lattice_complete_inventory_fragment import (
    INVENTORY_HEALTH_UNCHANGED,
    MAX_COMPLETE_INVENTORY_DEVICES,
    MAX_COMPLETE_INVENTORY_SITES,
    CompleteInventoryFragmentPublisher,
    InventoryDeviceObservation,
    InventorySiteObservation,
    _bounded_observations,
    _bounded_text,
    _optional_bool,
    _optional_text,
    _provider_identifier,
)


_ENPHASE_DEVICE_KINDS = frozenset(
    (
        "battery",
        "gateway",
        "inverter",
        "meter",
    )
)


@dataclass(frozen=True)
class EnphaseSiteObservation:
    """One site explicitly returned by Enphase discovery."""

    site_id: object
    online: Optional[bool] = None

    def __post_init__(self):
        """Normalize bounded provider-local site evidence."""
        site = InventorySiteObservation(
            site_id=self.site_id,
            online=self.online,
        )
        object.__setattr__(self, "site_id", site.site_id)
        object.__setattr__(self, "online", site.online)

    def inventory_site(self):
        """Return the generic immutable site observation."""
        return InventorySiteObservation(
            site_id=self.site_id,
            online=self.online,
        )


@dataclass(frozen=True)
class EnphaseDeviceObservation:
    """One device explicitly identified by Enphase discovery evidence."""

    device_id: object
    site_id: object
    kind: str
    api_verified_serial: Optional[str] = None
    model: Optional[str] = None
    online: Optional[bool] = None

    def __post_init__(self):
        """Normalize bounded evidence without inferring capabilities."""
        device_id = _provider_identifier(self.device_id, "device_id")
        site_id = _provider_identifier(self.site_id, "site_id")
        kind = _bounded_text(self.kind, "kind", 64).lower().replace("_", "-")
        if kind not in _ENPHASE_DEVICE_KINDS:
            raise ValueError(
                "kind must be one of {}".format(
                    ", ".join(sorted(_ENPHASE_DEVICE_KINDS)),
                )
            )
        serial = self.api_verified_serial
        if serial is not None:
            serial = _provider_identifier(
                serial,
                "api_verified_serial",
            ).upper()
        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(self, "site_id", site_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "api_verified_serial", serial)
        object.__setattr__(
            self,
            "model",
            _optional_text(self.model, "model"),
        )
        object.__setattr__(
            self,
            "online",
            _optional_bool(self.online, "online"),
        )

    def inventory_device(self):
        """Return the generic immutable device observation."""
        return InventoryDeviceObservation(
            device_id=self.device_id,
            site_id=self.site_id,
            kind=self.kind,
            model=self.model,
            online=self.online,
            verified_hardware_serial=self.api_verified_serial,
        )


def _exact_canonical_fields(raw_fields, canonical_fields):
    """Return whether exact built-in field values already match canonical."""
    return all(
        type(raw) is type(canonical) and raw == canonical
        for raw, canonical in zip(
            raw_fields,
            canonical_fields,
        )
    )


def _validated_enphase_site(site):
    """Return one exact canonical Enphase site reconstruction."""
    if type(site) is not EnphaseSiteObservation:
        raise ValueError("Enphase site observation has an invalid type")
    raw_fields = (
        site.site_id,
        site.online,
    )
    clean = EnphaseSiteObservation(
        site_id=raw_fields[0],
        online=raw_fields[1],
    )
    canonical_fields = (
        clean.site_id,
        clean.online,
    )
    if not _exact_canonical_fields(
        raw_fields,
        canonical_fields,
    ):
        raise ValueError(
            "Enphase site observation is not canonical",
        )
    return clean


def _validated_enphase_device(device):
    """Return one exact canonical Enphase device reconstruction."""
    if type(device) is not EnphaseDeviceObservation:
        raise ValueError("Enphase device observation has an invalid type")
    raw_fields = (
        device.device_id,
        device.site_id,
        device.kind,
        device.api_verified_serial,
        device.model,
        device.online,
    )
    clean = EnphaseDeviceObservation(
        device_id=raw_fields[0],
        site_id=raw_fields[1],
        kind=raw_fields[2],
        api_verified_serial=raw_fields[3],
        model=raw_fields[4],
        online=raw_fields[5],
    )
    canonical_fields = (
        clean.device_id,
        clean.site_id,
        clean.kind,
        clean.api_verified_serial,
        clean.model,
        clean.online,
    )
    if not _exact_canonical_fields(
        raw_fields,
        canonical_fields,
    ):
        raise ValueError(
            "Enphase device observation is not canonical",
        )
    return clean


class EnphaseInventoryFragmentPublisher(CompleteInventoryFragmentPublisher):
    """Default-off publisher for explicit complete Enphase inventories."""

    def __init__(self, provider_id, state_store, enabled=False):
        """Create an unwired Enphase inventory publisher."""
        super().__init__(
            provider_id,
            "Enphase Cloud Inventory",
            state_store,
            enabled=enabled,
        )

    def ingest_snapshot(
        self,
        discovery_generation,
        sites,
        devices,
        health=INVENTORY_HEALTH_UNCHANGED,
        feedback_token=None,
    ):
        """Normalize and publish one complete Enphase discovery generation."""
        if not self.enabled:
            return False
        sites = _bounded_observations(
            sites,
            "sites",
            EnphaseSiteObservation,
            MAX_COMPLETE_INVENTORY_SITES,
        )
        devices = _bounded_observations(
            devices,
            "devices",
            EnphaseDeviceObservation,
            MAX_COMPLETE_INVENTORY_DEVICES,
        )
        sites = tuple(_validated_enphase_site(site) for site in sites)
        devices = tuple(_validated_enphase_device(device) for device in devices)
        return self.ingest_inventory(
            discovery_generation,
            (EnphaseSiteObservation.inventory_site(site) for site in sites),
            (
                EnphaseDeviceObservation.inventory_device(
                    device,
                )
                for device in devices
            ),
            health=health,
            feedback_token=feedback_token,
        )
