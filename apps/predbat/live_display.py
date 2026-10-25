"""Provider adapters for a bounded, measured-only optional Hub display feed."""

import datetime
import hashlib
import math
import re

MAX_AGE = 300
MAX_POWER = 1000000


def selected_serials(value):
    """Require an explicit, unique physical inverter set instead of all devices."""
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (tuple, list)) or not 1 <= len(values) <= 16:
        raise ValueError("selected inverter set missing")
    if any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", item) for item in values):
        raise ValueError("invalid selected inverter identity")
    serials = sorted(item.upper() for item in values)
    if len(set(serials)) != len(serials):
        raise ValueError("duplicate selected inverter identity")
    return serials


def source_identity(provider, serials):
    """Bind the provider-independent wire feed to the selected physical set."""
    return hashlib.sha256((provider + "\n" + "\n".join(selected_serials(serials))).encode()).hexdigest()


def validated_binding(value, device_id, serials, enabled):
    """Whitelist server-issued display metadata or explicitly revoke prior opt-in."""
    disabled = {"enabled": False}
    try:
        if enabled is not True or not isinstance(value, dict) or set(value) != {"enabled", "device_id", "provider", "source_id"}:
            return disabled
        if not isinstance(device_id, str) or not re.fullmatch(r"pbgw_[a-f0-9]{12}", device_id):
            return disabled
        expected = {"enabled": True, "device_id": device_id, "provider": "givenergy", "source_id": source_identity("givenergy", serials)}
        if value.get("enabled") is not True or value != expected:
            return disabled
        return expected
    except (TypeError, ValueError):
        return disabled


def number(value, minimum, maximum):
    """Accept real finite measurements, including zero, without numeric coercion."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("missing or invalid measurement")
    return value


def gecloud_readings(component, serials, now):
    """Adapt complete, unmerged GE samples with explicit shared-CT aggregation.

    EMS/Gateway compositions and extra PV devices remain unavailable until their
    measurement provenance is proven. Multiple direct batteries need one known
    common grid meter, so grid/load are counted once. Battery/PV power is summed,
    and actual SoC is weighted by each battery's reported nominal Wh capacity.
    """
    devices = component.devices_dict
    if not isinstance(devices, dict) or devices.get("ems") or devices.get("gateway") or devices.get("pv"):
        raise ValueError("unsupported site composition")
    if selected_serials(devices.get("battery")) != serials:
        raise ValueError("selected inverter set differs from provider site")
    site_ids = devices.get("site_ids", {})
    sites = {site_ids.get(serial.lower()) for serial in serials}
    if None in sites or len(sites) != 1:
        raise ValueError("ambiguous provider site")
    if len(serials) > 1:
        meters = [devices.get("battery_meters", {}).get(serial.lower()) for serial in serials]
        if any(not isinstance(meter, list) or len(meter) != 1 or meter[0] is None for meter in meters) or any(meter != meters[0] for meter in meters):
            raise ValueError("ambiguous grid meter aggregation")
    samples = component.live_display_samples
    rows = []
    for serial in serials:
        raw = samples.get(serial.lower())
        if not isinstance(raw, dict):
            raise ValueError("no complete raw provider sample")
        stamp = datetime.datetime.fromisoformat(raw["time"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("source timestamp has no timezone")
        observed = int(stamp.timestamp())
        if observed < now - MAX_AGE or observed > now + 30:
            raise ValueError("source measurement is stale or future dated")
        battery = raw["battery"]
        info = component.info[serial.lower()]["info"]["battery"]
        capacity = number(info["nominal_capacity"], 0.001, 100000) * number(info["nominal_voltage"], 0.001, 10000)
        number(capacity, 1, 1000000)
        rows.append(
            {
                "observed": observed,
                "soc": number(battery["percent"], 0, 100),
                "capacity": capacity,
                # GE's raw battery sign is +discharge, firmware is +charge.
                "battery": -number(battery["power"], -MAX_POWER, MAX_POWER),
                "solar": number(raw["solar"]["power"], 0, MAX_POWER),
                "grid": number(raw["grid"]["power"], -MAX_POWER, MAX_POWER),
                "load": number(raw["consumption"], 0, MAX_POWER),
            }
        )
    if max(row["observed"] for row in rows) - min(row["observed"] for row in rows) > 60:
        raise ValueError("site samples are not contemporaneous")
    capacity = sum(row["capacity"] for row in rows)
    number(capacity, 1, 1000000)
    result = {
        "observed_at": min(row["observed"] for row in rows),
        "soc": round(sum(row["soc"] * row["capacity"] for row in rows) / capacity),
        "capacity_wh": round(capacity),
        "grid_w": round(rows[0]["grid"]),
        "load_w": round(rows[0]["load"]),
        "battery_w": round(sum(row["battery"] for row in rows)),
        "solar_w": round(sum(row["solar"] for row in rows)),
    }
    for key in ("battery_w", "solar_w", "grid_w", "load_w"):
        number(result[key], -MAX_POWER if key in ("battery_w", "grid_w") else 0, MAX_POWER)
    return result


def live_display_payload(base, device_id, configured_serials, now):
    """Return unavailable unless one supported, selected provider proves readings."""
    result = {"v": 1, "enabled": True, "device_id": device_id, "available": False}
    try:
        if not isinstance(device_id, str) or not re.fullmatch(r"pbgw_[a-f0-9]{12}", device_id):
            raise ValueError("invalid paired Hub identity")
        from components import COMPONENT_LIST

        components = base.components.components
        sources = [(name, component) for name, component in components.items() if component and COMPONENT_LIST.get(name, {}).get("inverter") and getattr(component, "gateway_display_only", False) is not True]
        if len(sources) != 1 or sources[0][0] != "gecloud":
            raise ValueError("unsupported or ambiguous selected provider")
        provider = "givenergy"
        serials = selected_serials(configured_serials)
        result.update(provider=provider, source_id=source_identity(provider, serials))
        result.update(gecloud_readings(sources[0][1], serials, now), available=True)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        # No partial readings or made-up zero. Firmware clears any previous live
        # values on this message, and also expires them without new messages.
        pass
    return result


def visibility_payload(preference, device_id, now):
    """Fresh device-bound presentation preference, never bus/control config."""
    if not isinstance(preference, dict) or preference.get("v") != 1:
        return None
    revision = preference.get("revision")
    instance = preference.get("instance_id")
    if (
        preference.get("device_id") != device_id
        or not isinstance(device_id, str)
        or not re.fullmatch(r"pbgw_[a-f0-9]{12}", device_id)
        or not isinstance(instance, str)
        or not re.fullmatch(r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}", instance)
        or type(revision) is not int
        or not 1 <= revision <= 2147483647
        or type(preference.get("enabled")) is not bool
        or type(now) is not int
        or now < 1700000000
    ):
        return None
    return {"v": 1, "device_id": device_id, "instance_id": instance, "revision": revision, "enabled": preference["enabled"], "issued_at": now, "expires_at": now + 120}
