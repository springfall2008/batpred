"""Sealed, one-shot destination transport observations, never activation authority."""

import hashlib
import asyncio
import math
import os
import re
import struct
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from control_handoff import ControlAttachFailed, ControlWriteGate

BATTERY_ARGUMENTS = (
    "user_id",
    "ge_cloud_direct",
    "ge_cloud_automatic",
    "ge_cloud_automatic_split_pv",
    "ge_cloud_automatic_split_shared",
    "ge_cloud_automatic_ems",
    "ge_cloud_serial",
    "ge_cloud_data",
    "gateway_device_id",
    "gateway_mqtt_host",
    "gateway_mqtt_port",
    "gateway_mqtt_auth_method",
    "gateway_inverter_serial",
    "gateway_display_only",
    "gateway_shared_ct",
    "gateway_integrate_power",
    "inverter_type",
    "num_inverters",
    "givtcp_rest",
    "auto_restart",
    "set_read_only",
)


def canonical_battery_value(value):
    """Encode exactly the versioned TypeScript manifest byte representation."""
    if value is None:
        return "n"
    if value is True:
        return "t"
    if value is False:
        return "f"
    if type(value) in (int, float):
        if not math.isfinite(value) or abs(value) > 9007199254740991:
            raise ValueError("Invalid manifest number")
        return "d" + struct.pack(">d", 0 if value == 0 else value).hex()
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="strict")
        # TypeScript measures UTF-16 code units for this bounded text field.
        if len(value.encode("utf-16-le")) // 2 > 4096:
            raise ValueError("Invalid manifest text")
        return "s{}:{}".format(len(encoded), value)
    if isinstance(value, list):
        if len(value) > 256:
            raise ValueError("Oversized manifest array")
        return "a{}:".format(len(value)) + "".join(canonical_battery_value(item) for item in value)
    if type(value) is dict:
        if len(value) > 256 or any(not isinstance(key, str) or not re.fullmatch(r"[\x20-\x7e]{1,128}", key) for key in value):
            raise ValueError("Invalid manifest keys")
        return "o{}:".format(len(value)) + "".join(canonical_battery_value(key) + canonical_battery_value(value[key]) for key in sorted(value))
    raise ValueError("Unsupported manifest value")


def loaded_battery_manifest(args, issued_manifest):
    """Project loaded transport args while retaining server-issued route references."""
    if type(issued_manifest) is not dict or set(issued_manifest) != {"schema", "provider", "routes", "credential_ref", "battery_args"} or type(issued_manifest.get("schema")) is not int or issued_manifest.get("schema") != 1:
        raise ValueError("Invalid issued manifest")
    return {"schema": 1, "provider": issued_manifest["provider"], "routes": issued_manifest["routes"], "credential_ref": issued_manifest["credential_ref"], "battery_args": {key: args[key] for key in BATTERY_ARGUMENTS if key in args}}


def battery_manifest_digest(manifest):
    """Hash only projected non-secret arguments and explicit credential references."""
    return hashlib.sha256(("predbat-battery-config-v1\n" + canonical_battery_value(manifest)).encode("utf-8")).hexdigest()


def _uuid(value):
    """Require a canonical, lowercase UUID string without coercion."""
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
        return str(parsed) == value and parsed.version in range(1, 9)
    except ValueError:
        return False


def _fresh_timestamp(value, max_age=300):
    """Require an explicitly zoned recent sample, including bounded future skew."""
    if not isinstance(value, str) or not re.search(r"(Z|[+-]\d{2}:\d{2})$", value):
        raise ValueError("Missing inverter timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    age = time.time() - stamp
    if not math.isfinite(stamp) or age > max_age or age < -30:
        raise ValueError("Stale inverter timestamp")
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def fresh_givenergy_observation(args, routes):
    """Read the actual loaded credential directly, with no cache or optimistic state."""
    api_key = args.get("ge_cloud_key")
    if args.get("ge_cloud_direct") is not True or not isinstance(api_key, str) or not api_key or args.get("givtcp_rest"):
        raise ValueError("Unsupported target transport")
    ref = routes[0]["gateway_ref"]
    if ref.get("kind") != "instance_secret" or ref.get("instance_id") != args.get("user_id") or ref.get("integration") != "givenergy" or ref.get("path") != "api_key":
        raise ValueError("Unsupported target credential reference")

    def read(path):
        """Perform only one read and never follow a credential-bearing redirect."""
        response = requests.get("https://api.givenergy.cloud/v1/" + path, headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"}, timeout=5, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError("Target read unavailable")
        value = response.json()
        if type(value) is not dict:
            raise ValueError("Invalid target response")
        return value

    expected = sorted(route["serial"] for route in routes)
    if not 1 <= len(expected) <= 16 or len(set(expected)) != len(expected) or any(route.get("manufacturer") != "givenergy" or route["gateway_ref"] != ref for route in routes):
        raise ValueError("Unsupported physical routes")
    discovery = read("communication-device")
    if type(discovery.get("data")) is not list or discovery.get("meta", {}).get("last_page") != 1:
        raise ValueError("Incomplete target discovery")
    serials = []
    for device in discovery["data"]:
        inverter = device.get("inverter")
        if type(inverter) is not dict or type(inverter.get("connections", {}).get("batteries")) is not list:
            raise ValueError("Incomplete target discovery")
        if inverter["connections"]["batteries"]:
            if any(name in inverter.get("info", {}).get("model", "").lower() for name in ("gateway", "plant ems", "gw2")):
                raise ValueError("Aggregate target adapter unavailable")
            serials.append(inverter["serial"].upper())
    if sorted(serials) != expected:
        raise ValueError("Target physical identities differ")
    # The current independent server adapter deliberately supports one battery.
    # Do not claim the later runtime dispatch map for a multi-inverter account.
    if len(expected) != 1:
        raise ValueError("Multi-inverter target adapter unavailable")
    data = read("inverter/{}/system-data/latest".format(expected[0])).get("data")
    if type(data) is not dict or data.get("serial", expected[0]).upper() != expected[0]:
        raise ValueError("Target telemetry identity differs")
    percent = data.get("battery", {}).get("percent")
    if type(percent) not in (float, int) or not math.isfinite(percent) or not 0 <= percent <= 100:
        raise ValueError("Target telemetry unavailable")
    return {"provider": "givenergy", "serials": expected, "sampled_at": _fresh_timestamp(data.get("time")), "checked_at": datetime.now(timezone.utc).isoformat(), "sealed": True, "pending": 0, "uncertain": 0}


def observe_loaded_target(base):
    """Seal before any component, claim the loaded binding, observe, then stop startup."""
    boot_id = str(uuid.uuid4())
    gate = ControlWriteGate("target-readiness", boot_id, 1, time.monotonic() + 1)
    gate.seal()
    base.control_write_gate = gate
    try:
        args = base.args
        envelope = args.get("control_readiness")
        if args.get("control_handoff") != {"enabled": True, "readiness": True} or type(envelope) is not dict:
            raise ValueError("Invalid readiness mode")
        if envelope.get("state") != "challenge" or envelope.get("activation_available") is not False or envelope.get("boot_id") is not None:
            raise ValueError("Invalid readiness envelope")
        for name in ("challenge_id", "transition_id", "target_epoch"):
            if not _uuid(envelope.get(name)):
                raise ValueError("Invalid readiness identity")
        version = args.get("saas_config_version")
        if type(version) is not int or not 1 <= version <= 2147483647 or type(envelope.get("config_version")) is not int or version != envelope["config_version"]:
            raise ValueError("Invalid loaded readiness revision")
        if not isinstance(envelope.get("nonce"), str) or not re.fullmatch(r"[a-f0-9]{64}", envelope["nonce"]):
            raise ValueError("Invalid readiness nonce")
        manifest = loaded_battery_manifest(args, envelope["manifest"])
        digest = battery_manifest_digest(manifest)
        if manifest != envelope["manifest"] or digest != envelope.get("manifest_digest") or manifest["provider"] != envelope.get("provider") or manifest["routes"] != envelope.get("routes"):
            raise ValueError("Loaded readiness projection differs")
        if args.get("auto_restart") or manifest["provider"] not in ("givenergy", "predbat_gateway"):
            raise ValueError("Target transport observation unavailable")
        expiry = datetime.fromisoformat(envelope["expires_at"].replace("Z", "+00:00")).timestamp()
        if expiry <= time.time() or expiry > time.time() + 120:
            raise ValueError("Invalid readiness deadline")
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        parsed = urlsplit(url)
        if not key or parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Readiness service credentials unavailable")
        binding = {
            "transition_id": envelope["transition_id"],
            "challenge_id": envelope["challenge_id"],
            "target_epoch": envelope["target_epoch"],
            "nonce": envelope["nonce"],
            "loaded_config_version": version,
            "manifest": manifest,
            "manifest_digest": digest,
        }

        def send(action, observation=None):
            """Post one bounded observation request, preserving permanent local sealing."""
            if time.time() >= expiry:
                raise ValueError("Readiness deadline expired")
            payload = {"action": action, "challenge_id": envelope["challenge_id"], "boot_id": boot_id, "binding": binding, "loaded_credential": args.get("ge_cloud_key") if manifest["provider"] == "givenergy" else args.get("gateway_mqtt_token")}
            if observation is not None:
                payload["observation"] = observation
            response = requests.post(url.rstrip("/") + "/functions/v1/engine-battery-readiness", json=payload, headers={"Authorization": "Bearer " + key}, timeout=min(10, expiry - time.time()), allow_redirects=False)
            result = response.json()
            if response.status_code != 200 or type(result) is not dict or result.get("state") != ("attached" if action == "attach" else "observed") or result.get("sealed") is not True or result.get("activation_available") is not False:
                raise ValueError("Readiness service refused")
            if result.get("target_epoch") != envelope["target_epoch"] or result.get("boot_id") != boot_id or result.get("challenge_id") != envelope["challenge_id"] or time.time() >= expiry:
                raise ValueError("Readiness response binding differs")
            return result

        attached = send("attach")
        if manifest["provider"] == "givenergy":
            observation = fresh_givenergy_observation(args, manifest["routes"])
        else:
            from gateway import GatewayMQTT

            observation = asyncio.run(GatewayMQTT.observe_readonly_transport(base, attached.get("transport_wire"), attached.get("transport_control"), min(10, expiry - time.time())))
        send("observe", observation)
    except Exception:
        raise ControlAttachFailed("Sealed target observation unavailable; activation unavailable") from None
    # This process never constructs components or runs the planner. The recorded
    # observation is a bounded step towards readiness, not permission to control.
    raise ControlAttachFailed("Sealed target observation recorded; activation unavailable")
