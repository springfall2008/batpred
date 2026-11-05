"""Measured provider provenance and real optional MQTT publisher regressions."""

import asyncio
import copy
import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gecloud import GECloudDirect
from live_display import gecloud_readings, live_display_payload, selected_serials, source_identity, validated_binding
from tests.test_gateway import TestPublishPredbatData as _PublisherFixture

NOW = 1800000000
DEVICE = "pbgw_0123456789ab"


def provider(serials=("INV001",), soc=0):
    """Make complete synthetic API observations, including valid idle/empty data."""
    stamp = datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc).isoformat()
    raw = {"time": stamp, "battery": {"percent": soc, "power": 0}, "solar": {"power": 0}, "grid": {"power": 0}, "consumption": 0}
    return SimpleNamespace(
        devices_dict={"battery": list(serials), "site_ids": {s.lower(): 7 for s in serials}, "battery_meters": {s.lower(): ["CT001"] for s in serials}},
        live_display_samples={s.lower(): copy.deepcopy(raw) for s in serials},
        info={s.lower(): {"info": {"battery": {"nominal_capacity": 100, "nominal_voltage": 50}}} for s in serials},
    )


def base_for(component, name="gecloud"):
    """Wrap a provider in the actual component registry shape."""
    return SimpleNamespace(components=SimpleNamespace(components={name: component}))


def test_zero_is_a_complete_actual_reading():
    """Empty battery and idle powers are valid when the source is proven."""
    payload = live_display_payload(base_for(provider()), DEVICE, "INV001", NOW)
    assert payload["available"] and payload["soc"] == 0
    assert all(payload[key] == 0 for key in ("battery_w", "solar_w", "grid_w", "load_w"))
    assert payload["source_id"] == source_identity("givenergy", ["inv001"])


def test_incomplete_or_invalid_raw_sample_is_unavailable():
    """Sensor defaults and merged historical fields cannot become actuals."""
    for field, value in [("battery", None), ("time", None), ("consumption", None), ("consumption", True), ("consumption", float("nan"))]:
        component = provider()
        component.live_display_samples["inv001"][field] = value
        result = live_display_payload(base_for(component), DEVICE, "INV001", NOW)
        assert not result["available"] and "soc" not in result


def test_source_time_is_bounded():
    """A newly received stale/future sample is unavailable."""
    for offset in (-301, 31):
        component = provider()
        component.live_display_samples["inv001"]["time"] = datetime.datetime.fromtimestamp(NOW + offset, datetime.timezone.utc).isoformat()
        assert not live_display_payload(base_for(component), DEVICE, "INV001", NOW)["available"]


def test_selected_identity_and_topology_must_be_proven():
    """Reject account-wide, unsupported and ambiguous provider selections."""
    component = provider()
    for selected in (None, [], ["INV001", "inv001"], "OTHER"):
        assert not live_display_payload(base_for(component), DEVICE, selected, NOW)["available"]
    for field in ("gateway", "ems", "pv"):
        unsupported = provider()
        unsupported.devices_dict[field] = ["OTHER"]
        assert not live_display_payload(base_for(unsupported), DEVICE, "INV001", NOW)["available"]
    assert not live_display_payload(base_for(component, "fox"), DEVICE, "INV001", NOW)["available"]
    base = base_for(component)
    base.components.components["gateway"] = SimpleNamespace(gateway_display_only=False)
    assert not live_display_payload(base, DEVICE, "INV001", NOW)["available"]


def test_explicit_shared_ct_aggregation():
    """Count common site load/grid once, sum battery/PV, capacity-weight actual SoC."""
    component = provider(("INV001", "INV002"), soc=20)
    component.info["inv002"]["info"]["battery"]["nominal_capacity"] = 200
    component.live_display_samples["inv002"]["battery"] = {"percent": 80, "power": 300}
    for raw in component.live_display_samples.values():
        raw["consumption"] = 400
        raw["grid"]["power"] = -100
        raw["solar"]["power"] = 50
    result = gecloud_readings(component, selected_serials(["INV002", "INV001"]), NOW)
    assert result == {"observed_at": NOW, "soc": 60, "capacity_wh": 15000, "grid_w": -100, "load_w": 400, "battery_w": -300, "solar_w": 100}
    component.devices_dict["battery_meters"]["inv002"] = ["OTHER"]
    assert not live_display_payload(base_for(component), DEVICE, ["INV001", "INV002"], NOW)["available"]


def test_raw_capture_does_not_inherit_old_fields():
    """Exercise the actual fetch method: normal control merge stays, display rejects it."""
    component = provider()
    raw = copy.deepcopy(component.live_display_samples["inv001"])
    raw["battery"]["percent"] = None
    component.async_get_inverter_data_retry = AsyncMock(return_value=raw)
    merged = asyncio.run(GECloudDirect.async_get_inverter_status(component, "INV001", {"battery": {"percent": 55}}))
    assert merged["battery"]["percent"] == 55
    assert component.live_display_samples["inv001"]["battery"]["percent"] is None
    assert not live_display_payload(base_for(component), DEVICE, "INV001", NOW)["available"]
    component.async_get_inverter_data_retry.return_value = None
    asyncio.run(GECloudDirect.async_get_inverter_status(component, "INV001", merged))
    assert component.live_display_samples["inv001"] is None


def published_gateway(enabled=True, parked=True):
    """Use the actual publisher with its existing isolated state reader fixture."""
    gw = _PublisherFixture()._make_gateway()
    gw.gateway_device_id = DEVICE
    gw.gateway_inverter_serial = "INV001"
    gw.gateway_display_only = parked
    gw.gateway_live_display = enabled
    gw.gateway_live_display_binding = {"enabled": True, "device_id": DEVICE, "provider": "givenergy", "source_id": source_identity("givenergy", ["INV001"])}
    gw._last_live_display_publish = 0
    gw.base = base_for(provider())
    gw._topic_base = "predbat/devices/" + DEVICE
    return gw


def test_server_binding_validation_and_metadata_publisher():
    """Only exact whitelist metadata crosses the dedicated read-only path."""
    from gateway import GatewayMQTT

    gw = published_gateway()
    binding = gw.gateway_live_display_binding
    assert validated_binding(binding, DEVICE, "INV001", True) == binding
    assert source_identity("givenergy", ["CE2119G197", "CE2105G024"]) == "3915601d8c60cbe0bb8768f509be81c928e8f5df16cc42dff14efdf0f4bce2f6"
    for bad in (None, {**binding, "source_id": "a" * 64}, {**binding, "device_id": "pbgw_aaaaaaaaaaaa"}, {**binding, "provider": "foxess"}, {**binding, "poll_interval_ms": 1}, {**binding, "enabled": 1}):
        assert validated_binding(bad, DEVICE, "INV001", True) == {"enabled": False}
    gw._mqtt_connected = True
    gw._mqtt_client = SimpleNamespace(publish=AsyncMock())
    gw._loop = None
    gw._last_live_binding = None
    gw._last_live_binding_publish = 0
    with patch("gateway.time.time", return_value=NOW):
        asyncio.run(gw._publish_display_binding())
        asyncio.run(gw._publish_display_binding())
        assert gw._mqtt_client.publish.await_count == 1
        asyncio.run(gw._publish_display_binding(force=True))  # reconnect bypasses dedup
        assert gw._mqtt_client.publish.await_count == 2
    topic, data = gw._mqtt_client.publish.await_args.args
    assert topic == "predbat/devices/" + DEVICE + "/config"
    assert json.loads(data) == {"live_display": binding}
    assert gw._mqtt_client.publish.await_args.kwargs == {"qos": 1, "retain": False}
    with patch("gateway.time.time", return_value=NOW + 61):
        asyncio.run(gw._publish_display_binding())
        assert gw._mqtt_client.publish.await_count == 3
    gw.gateway_live_display = False
    asyncio.run(gw._publish_display_binding())
    assert json.loads(gw._mqtt_client.publish.await_args.args[1]) == {"live_display": {"enabled": False}}
    gw.gateway_display_only = False
    gw._mqtt_client.publish.reset_mock()
    asyncio.run(gw._publish_display_binding(force=True))
    gw._mqtt_client.publish.assert_not_awaited()
    gw.gateway_display_only = True
    # General config/control remains prohibited through the original raw path.
    asyncio.run(GatewayMQTT._publish_raw(gw, topic, b'{"poll_interval_ms":1}'))
    gw._mqtt_client.publish.assert_not_awaited()


def test_invalid_binding_fails_closed_and_failed_publish_retries():
    """An unbound feed is unavailable and failed metadata sends are not deduplicated."""
    gw = published_gateway()
    gw.gateway_live_display_binding = None
    with patch("gateway.time.time", return_value=NOW):
        asyncio.run(gw._publish_predbat_data())
    assert not json.loads(gw._published[0][1])["live_display"]["available"]
    gw._mqtt_connected = True
    gw._mqtt_client = SimpleNamespace(publish=AsyncMock(side_effect=RuntimeError("synthetic failure")))
    gw._loop = None
    gw._last_live_binding = None
    gw._last_live_binding_publish = 0
    asyncio.run(gw._publish_display_binding())
    assert gw._last_live_binding is None
    gw._mqtt_client.publish.side_effect = None
    asyncio.run(gw._publish_display_binding())
    assert gw._mqtt_client.publish.await_count == 2
    assert gw._last_live_binding == {"live_display": {"enabled": False}}


def test_real_publisher_is_opt_in_parked_bounded_and_heartbeats():
    """Cover the real producer path and export its exact synthetic wire contract."""
    for enabled, parked in ((False, True), (True, False)):
        gw = published_gateway(enabled, parked)
        asyncio.run(gw._publish_predbat_data())
        assert "live_display" not in json.loads(gw._published[0][1])
    gw = published_gateway()
    with patch("gateway.time.time", return_value=NOW):
        asyncio.run(gw._publish_predbat_data())
        asyncio.run(gw._publish_predbat_data())
        assert len(gw._published) == 1
    with patch("gateway.time.time", return_value=NOW + 61):
        asyncio.run(gw._publish_predbat_data())
    assert len(gw._published) == 2
    topic, data, retain = gw._published[0]
    assert retain and len(topic.encode()) + len(data) + 16 <= 1400
    assert json.loads(data)["live_display"]["available"]


def write_wire_fixture(path, backend_path=None):
    """Export actual publisher/config bytes, optionally starting from SaaS output."""
    gw = published_gateway()
    if backend_path:
        with open(backend_path, encoding="utf-8") as source:
            config = json.load(source)
        serials = config["gateway_inverter_serial"]
        gw.base = base_for(provider(serials))
        gw.args = {}
        gw.initialize(
            gateway_device_id=config["gateway_device_id"],
            mqtt_host=config["gateway_mqtt_host"],
            gateway_display_only=config["gateway_display_only"],
            gateway_inverter_serial=serials,
            gateway_live_display=config["gateway_live_display"],
            gateway_live_display_binding=config["gateway_live_display_binding"],
        )
    serial = selected_serials(gw.gateway_inverter_serial)[0].lower()
    sample = gw.base.components.components["gecloud"].live_display_samples[serial]
    sample["battery"]["power"] = 321
    sample["grid"]["power"] = -456
    sample["solar"]["power"] = 78
    sample["consumption"] = 99
    gw._mqtt_connected = True
    gw._mqtt_client = SimpleNamespace(publish=AsyncMock())
    gw._loop = None
    gw._last_live_binding = None
    gw._last_live_binding_publish = 0
    with patch("gateway.time.time", return_value=NOW):
        asyncio.run(gw._publish_display_binding(force=True))
        asyncio.run(gw._publish_predbat_data())
    topic, data, _ = gw._published[0]
    metadata_topic, metadata_payload = gw._mqtt_client.publish.await_args.args
    assert metadata_topic == "predbat/devices/" + DEVICE + "/config"
    assert not gw._mqtt_client.publish.await_args.kwargs["retain"]
    config = json.loads(metadata_payload)["live_display"]
    assert config["enabled"]
    with open(path, "w", encoding="utf-8") as output:
        json.dump({"topic": topic, "payload": data.decode(), "config": config}, output)


def run_live_display_tests():
    """Run the same contract assertions through the existing CLI test registry."""
    test_zero_is_a_complete_actual_reading()
    test_incomplete_or_invalid_raw_sample_is_unavailable()
    test_source_time_is_bounded()
    test_selected_identity_and_topology_must_be_proven()
    test_explicit_shared_ct_aggregation()
    test_raw_capture_does_not_inherit_old_fields()
    test_real_publisher_is_opt_in_parked_bounded_and_heartbeats()
    test_server_binding_validation_and_metadata_publisher()
    test_invalid_binding_fails_closed_and_failed_publish_retries()


if __name__ == "__main__":
    import sys

    write_wire_fixture(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
