# -----------------------------------------------------------------------------
# Predbat Home Battery System - Deye complete-inventory Lattice adapter
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure Deye station/inverter inventory normalization.

The live Deye component is intentionally not imported.  A future gated seam
may pass explicit station/device observations here only after a successful
cloud discovery.  This module publishes no TOU, scheduler, write, endpoint,
feature, authentication, or control claims.

Station and device IDs stay provider-local.  The separate
``api_verified_serial`` field is the only input allowed to become a strong
cross-provider hardware identity.
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
    _optional_bool,
    _optional_text,
    _provider_identifier,
)


@dataclass(frozen=True)
class DeyeStationObservation:
    """One explicit Deye station returned by cloud discovery."""

    station_id: object
    online: Optional[bool] = None

    def __post_init__(self):
        """Normalize only provider-local station state."""
        site = InventorySiteObservation(
            site_id=self.station_id,
            online=self.online,
        )
        object.__setattr__(self, "station_id", site.site_id)
        object.__setattr__(self, "online", site.online)

    def inventory_site(self):
        """Return the generic immutable site observation."""
        return InventorySiteObservation(
            site_id=self.station_id,
            online=self.online,
        )


@dataclass(frozen=True)
class DeyeInverterObservation:
    """One explicit Deye inverter assigned to a discovered station."""

    device_id: object
    station_id: object
    api_verified_serial: Optional[str] = None
    model: Optional[str] = None
    online: Optional[bool] = None

    def __post_init__(self):
        """Normalize bounded metadata without inferring any capabilities."""
        device_id = _provider_identifier(self.device_id, "device_id")
        station_id = _provider_identifier(self.station_id, "station_id")
        serial = self.api_verified_serial
        if serial is not None:
            serial = _provider_identifier(
                serial,
                "api_verified_serial",
            ).upper()
        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(self, "station_id", station_id)
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
        """Return the generic immutable inverter observation."""
        return InventoryDeviceObservation(
            device_id=self.device_id,
            site_id=self.station_id,
            kind="inverter",
            model=self.model,
            online=self.online,
            verified_hardware_serial=self.api_verified_serial,
        )


def _validated_deye_station(station):
    """Return one exact canonical Deye station reconstruction."""
    if type(station) is not DeyeStationObservation:
        raise ValueError("Deye station observation has an invalid type")
    raw_station_id = station.station_id
    raw_online = station.online
    clean = DeyeStationObservation(
        station_id=raw_station_id,
        online=raw_online,
    )
    if (raw_station_id, raw_online) != (
        clean.station_id,
        clean.online,
    ):
        raise ValueError("Deye station observation is not canonical")
    return clean


def _validated_deye_inverter(inverter):
    """Return one exact canonical Deye inverter reconstruction."""
    if type(inverter) is not DeyeInverterObservation:
        raise ValueError("Deye inverter observation has an invalid type")
    raw_device_id = inverter.device_id
    raw_station_id = inverter.station_id
    raw_serial = inverter.api_verified_serial
    raw_model = inverter.model
    raw_online = inverter.online
    clean = DeyeInverterObservation(
        device_id=raw_device_id,
        station_id=raw_station_id,
        api_verified_serial=raw_serial,
        model=raw_model,
        online=raw_online,
    )
    if (
        raw_device_id,
        raw_station_id,
        raw_serial,
        raw_model,
        raw_online,
    ) != (
        clean.device_id,
        clean.station_id,
        clean.api_verified_serial,
        clean.model,
        clean.online,
    ):
        raise ValueError("Deye inverter observation is not canonical")
    return clean


class DeyeInventoryFragmentPublisher(CompleteInventoryFragmentPublisher):
    """Default-off publisher for explicit complete Deye inventories."""

    def __init__(self, provider_id, state_store, enabled=False):
        """Create an unwired Deye inventory publisher."""
        super().__init__(
            provider_id,
            "Deye Cloud Inventory",
            state_store,
            enabled=enabled,
        )

    def ingest_snapshot(
        self,
        discovery_generation,
        stations,
        inverters,
        health=INVENTORY_HEALTH_UNCHANGED,
        feedback_token=None,
    ):
        """Normalize and publish one complete Deye discovery generation."""
        if not self.enabled:
            return False
        stations = _bounded_observations(
            stations,
            "stations",
            DeyeStationObservation,
            MAX_COMPLETE_INVENTORY_SITES,
        )
        inverters = _bounded_observations(
            inverters,
            "inverters",
            DeyeInverterObservation,
            MAX_COMPLETE_INVENTORY_DEVICES,
        )
        stations = tuple(_validated_deye_station(station) for station in stations)
        inverters = tuple(_validated_deye_inverter(inverter) for inverter in inverters)
        return self.ingest_inventory(
            discovery_generation,
            (DeyeStationObservation.inventory_site(station) for station in stations),
            (
                DeyeInverterObservation.inventory_device(
                    inverter,
                )
                for inverter in inverters
            ),
            health=health,
            feedback_token=feedback_token,
        )
