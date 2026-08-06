# cspell:ignore autoconfig XIAO
# -----------------------------------------------------------------------------
# Predbat Home Battery System - pure live provider input adapters
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Detach live Gateway and GE Cloud state into Lattice mapper inputs.

This module deliberately has no component lifecycle, persistence, publication,
or configuration side effects.  Runtime wiring can call these helpers after a
successful provider read and hand their results to the existing pure fragment
publishers.
"""

from dataclasses import dataclass

from gecloud_autoconfig import GECloudAutoConfigInput, normalize_register_name
from lattice_ge_cloud_fragment import GECloudDeviceSnapshot


class LiveProviderInputError(ValueError):
    """Live provider state cannot be represented safely by the current slice."""


# Stable values from gateway_status.proto.  Keeping the adapter duck-typed
# avoids importing generated protobuf code into this pure conversion seam.
_GIVENERGY_EMS_TYPE = 7
_GIVENERGY_GATEWAY_TYPE = 8


@dataclass(frozen=True)
class GatewayInverterInput:
    """Detached fields consumed by ``compile_gateway_auto_config``."""

    serial: str
    kind: str
    primary: bool
    battery_present: bool
    battery_capacity_wh: int
    ems_num_inverters: int
    model: str


@dataclass(frozen=True)
class GatewayLiveInput:
    """One detached mapper input and matching synthetic topology fragment."""

    inverters: tuple
    topology_fragment: object


@dataclass(frozen=True)
class GECloudLiveInput:
    """Detached GE auto-config policy input and provider discovery devices."""

    auto_config: GECloudAutoConfigInput
    devices: tuple


def _field(value, name, default=None):
    """Read one field from a mapping or proto-like object."""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _iterable_field(value, name):
    """Detach one repeated field and report malformed provider state clearly."""
    items = _field(value, name, ())
    try:
        return tuple(items)
    except TypeError as exc:
        raise LiveProviderInputError("{} must be iterable".format(name)) from exc


def _required_text(value, name):
    """Normalize a required provider identity."""
    if not isinstance(value, str) or not value.strip():
        raise LiveProviderInputError("{} must be a non-empty string".format(name))
    return value.strip()


def _optional_text(value):
    """Normalize provider text while preserving absence."""
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _non_negative_int(value, name):
    """Normalize a non-negative integer without accepting booleans."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LiveProviderInputError("{} must be a non-negative integer".format(name))
    return value


def _gateway_kind(inverter_type):
    """Map a numeric or named Gateway enum value to the pure mapper kind."""
    normalized = str(inverter_type).strip().upper()
    if inverter_type == _GIVENERGY_EMS_TYPE or normalized in (
        "EMS",
        "INVERTER_TYPE_GIVENERGY_EMS",
    ):
        return "ems"
    if inverter_type == _GIVENERGY_GATEWAY_TYPE or normalized in (
        "GATEWAY",
        "INVERTER_TYPE_GIVENERGY_GATEWAY",
    ):
        return "gateway"
    return "inverter"


def _message_present(message):
    """Match protobuf ``ByteSize`` presence while supporting test doubles."""
    if message is None:
        return False
    byte_size = getattr(message, "ByteSize", None)
    if callable(byte_size):
        return byte_size() > 0
    if isinstance(message, dict):
        return bool(message)
    return bool(message)


def _gateway_inverter(inverter):
    """Detach one proto-like inverter entry."""
    serial = _required_text(_field(inverter, "serial"), "Gateway inverter serial")
    battery = _field(inverter, "battery")
    ems = _field(inverter, "ems")
    capacity = _field(battery, "capacity_wh", 0) if battery is not None else 0
    ems_count = _field(ems, "num_inverters", 0) if ems is not None else 0
    return GatewayInverterInput(
        serial=serial,
        kind=_gateway_kind(_field(inverter, "type", "inverter")),
        primary=bool(_field(inverter, "primary", False)),
        battery_present=_message_present(battery),
        battery_capacity_wh=_non_negative_int(capacity or 0, "battery capacity_wh"),
        ems_num_inverters=_non_negative_int(ems_count or 0, "EMS num_inverters"),
        model=str(_field(inverter, "model", "") or ""),
    )


def _gateway_node(inverter, provider_id):
    """Build one stable provider-owned v0.3 topology node."""
    serial = inverter.serial.upper()
    topology_kind = "gateway" if inverter.kind == "gateway" else "inverter"
    return {
        "id": "gateway:{}".format(serial),
        "kind": topology_kind,
        "attributes": {"serial": serial},
        "accessPaths": [
            {
                "id": "gateway-mqtt",
                "provider": provider_id,
                "preference": 10,
            }
        ],
        "capabilities": [],
    }


def gateway_status_to_lattice_input(
    status,
    provider_id="predbat-gateway",
    document_version=None,
):
    """Convert one non-empty Gateway status into mapper and topology inputs.

    Plant EMS and EV discovery fail closed because their legacy side effects are
    outside the current XIAO/Gateway auto-config slice.
    """
    provider_id = _required_text(provider_id, "provider_id")
    inverters = tuple(_gateway_inverter(item) for item in _iterable_field(status, "inverters"))
    if not inverters:
        raise LiveProviderInputError("Gateway status contains no inverters")
    if _iterable_field(status, "ev_chargers"):
        raise LiveProviderInputError("Gateway EV discovery is outside the Lattice auto-config slice")
    if any(item.kind == "ems" for item in inverters):
        raise LiveProviderInputError("Gateway EMS discovery requires the multi-battery coordinator model")

    serials = [item.serial.upper() for item in inverters]
    if len(serials) != len(set(serials)):
        raise LiveProviderInputError("Gateway status contains duplicate inverter serials")
    inverters = tuple(sorted(inverters, key=lambda item: item.serial.upper()))
    topology = {
        "topologyVersion": "0.3.0",
        "scope": "fragment",
        "producer": {
            "name": "PredBat Gateway",
            "provider": provider_id,
            "authority": 10,
        },
        "nodes": [_gateway_node(item, provider_id) for item in inverters],
    }
    if document_version is not None:
        topology["docVersion"] = _non_negative_int(
            document_version,
            "document_version",
        )
    return GatewayLiveInput(inverters, topology)


def _ge_device_kinds(devices):
    """Return one collision-checked serial-to-kind lookup."""
    result = {}
    groups = (
        ("battery", "battery-inverter"),
        ("pv", "pv-inverter"),
        ("gateway", "gateway"),
        ("ems", "ems"),
    )
    for key, kind in groups:
        raw = devices.get(key, ())
        values = (raw,) if key in ("gateway", "ems") and raw else tuple(raw or ())
        for value in values:
            serial = _required_text(value, "GE Cloud {} serial".format(key))
            normalized = serial.upper()
            previous = result.get(normalized)
            if previous is not None and previous[1] != kind:
                raise LiveProviderInputError("GE Cloud serial {} has conflicting device kinds".format(serial))
            result[normalized] = (serial, kind)
    return result


def _ge_info(info, serial):
    """Read one GE inverter info record case-insensitively."""
    record = info.get(serial)
    if record is None:
        record = info.get(serial.lower())
    if record is None:
        record = info.get(serial.upper())
    return record if isinstance(record, dict) else {}


def _ge_model(info, serial):
    """Mirror the legacy ``self.info[serial]['info']['model']`` lookup."""
    record = _ge_info(info, serial)
    details = record.get("info") or {}
    return str(details.get("model", "") or "") if isinstance(details, dict) else ""


def _ge_register_names(settings, serial):
    """Detach the HA-normalized register-name set for one device."""
    registers = settings.get(serial)
    if registers is None:
        registers = settings.get(serial.lower())
    if registers is None:
        registers = settings.get(serial.upper(), {})
    if not isinstance(registers, dict):
        raise LiveProviderInputError("GE Cloud settings for {} must be a mapping".format(serial))
    names = []
    for setting in registers.values():
        if not isinstance(setting, dict):
            raise LiveProviderInputError("GE Cloud setting for {} must be a mapping".format(serial))
        name = setting.get("name", "")
        if name:
            names.append(normalize_register_name(str(name)))
    return tuple(sorted(set(names)))


def _bool_flag(value, name):
    """Require explicit boolean runtime flags."""
    if not isinstance(value, bool):
        raise LiveProviderInputError("{} must be a boolean".format(name))
    return value


def gecloud_state_to_lattice_input(
    devices,
    settings,
    info,
    prefix="predbat",
    load_today_ignore=False,
    split_pv=False,
    split_ct=False,
    shared_ct=False,
):
    """Convert detached GE discovery/settings/info into existing pure inputs."""
    if not isinstance(devices, dict):
        raise LiveProviderInputError("GE Cloud devices must be a mapping")
    if not isinstance(settings, dict) or not isinstance(info, dict):
        raise LiveProviderInputError("GE Cloud settings and info must be mappings")
    prefix = _required_text(prefix, "prefix")
    batteries = tuple(devices.get("battery") or ())
    if not batteries:
        raise LiveProviderInputError("GE Cloud discovery contains no batteries")
    batteries = tuple(_required_text(item, "GE Cloud battery serial") for item in batteries)
    if len({item.upper() for item in batteries}) != len(batteries):
        raise LiveProviderInputError("GE Cloud discovery contains duplicate battery serials")
    pv = tuple(_required_text(item, "GE Cloud PV serial") for item in (devices.get("pv") or ()))
    ems = _optional_text(devices.get("ems"))
    gateway = _optional_text(devices.get("gateway"))
    kinds = _ge_device_kinds(devices)

    referenced = set(batteries + pv)
    if ems:
        referenced.add(ems)
    if gateway:
        referenced.add(gateway)
    register_names = tuple((serial, _ge_register_names(settings, serial)) for serial in sorted(referenced, key=str.upper))

    meters = devices.get("battery_meters") or {}
    if not isinstance(meters, dict):
        raise LiveProviderInputError("GE Cloud battery_meters must be a mapping")
    battery_meters = tuple((serial, tuple(meters.get(serial, meters.get(serial.lower(), ())))) for serial in batteries)
    models = tuple((serial, _ge_model(info, serial)) for serial in batteries)
    config = GECloudAutoConfigInput(
        batteries=batteries,
        ems=ems,
        gateway=gateway,
        pv=pv,
        battery_meters=battery_meters,
        register_names=register_names,
        models=models,
        prefix=prefix,
        load_today_ignore=_bool_flag(load_today_ignore, "load_today_ignore"),
        split_pv=_bool_flag(split_pv, "split_pv"),
        split_ct=_bool_flag(split_ct, "split_ct"),
        shared_ct=_bool_flag(shared_ct, "shared_ct"),
    )

    snapshots = []
    for normalized, (serial, kind) in sorted(kinds.items()):
        record = _ge_info(info, serial)
        snapshots.append(
            GECloudDeviceSnapshot(
                serial=normalized,
                kind=kind,
                model=_ge_model(info, serial) or None,
                site_id=record.get("site_id"),
                uuid=_optional_text(record.get("uuid")),
            )
        )
    return GECloudLiveInput(config, tuple(snapshots))
