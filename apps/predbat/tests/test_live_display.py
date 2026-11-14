"""Measured provider provenance and real optional MQTT publisher regressions."""

import asyncio
import contextlib
import copy
import datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


@contextlib.contextmanager
def clock_at(gw, now):
    """Give the gateway a clock reading now on its wall clock and, on a different scale, its monotonic clock for the block, then take it away."""
    gw.base.component_clock = SimpleNamespace(time=lambda: now, monotonic=lambda: now - NOW + 5000.0, sleep=asyncio.sleep)
    try:
        yield
    finally:
        del gw.base.component_clock


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
    gw.component_loop = None
    gw._last_live_binding = None
    gw._last_live_binding_publish = 0
    with clock_at(gw, NOW):
        asyncio.run(gw._publish_display_binding())
        asyncio.run(gw._publish_display_binding())
        assert gw._mqtt_client.publish.await_count == 1
        asyncio.run(gw._publish_display_binding(force=True))  # reconnect bypasses dedup
        assert gw._mqtt_client.publish.await_count == 2
    topic, data = gw._mqtt_client.publish.await_args.args
    assert topic == "predbat/devices/" + DEVICE + "/config"
    assert json.loads(data) == {"live_display": binding}
    assert gw._mqtt_client.publish.await_args.kwargs == {"qos": 1, "retain": False}
    with clock_at(gw, NOW + 61):
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
    with clock_at(gw, NOW):
        asyncio.run(gw._publish_predbat_data())
    assert not json.loads(gw._published[0][1])["live_display"]["available"]
    gw._mqtt_connected = True
    gw._mqtt_client = SimpleNamespace(publish=AsyncMock(side_effect=RuntimeError("synthetic failure")))
    gw.component_loop = None
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
    with clock_at(gw, NOW):
        asyncio.run(gw._publish_predbat_data())
        asyncio.run(gw._publish_predbat_data())
        assert len(gw._published) == 1
    with clock_at(gw, NOW + 61):
        asyncio.run(gw._publish_predbat_data())
    assert len(gw._published) == 2
    topic, data, retain = gw._published[0]
    assert retain and len(topic.encode()) + len(data) + 16 <= 1400
    assert json.loads(data)["live_display"]["available"]


def test_screen_off_revokes_the_live_binding():
    """With the screen turned off the Hub is told to drop the feed, not to keep waiting for it."""
    gw = published_gateway()
    assert gw._live_display_binding()["enabled"] is True
    gw.gateway_display_enabled = False
    assert gw._live_display_binding() == {"enabled": False}


def test_status_strings_fit_the_firmware_buffers_in_bytes():
    """A pound sign is two bytes: strings are cut by UTF-8 bytes, never mid-character."""
    from gateway import truncate_utf8

    assert truncate_utf8("£" * 40, 63) == "£" * 31
    assert truncate_utf8("abc", 15) == "abc"
    assert truncate_utf8("a" + "£" * 10, 4) == "a£"
    gw = published_gateway()
    real = gw.get_state_wrapper

    def states(entity_id, default=None, attribute=None):
        """Return long non-ASCII status text, and the fixture's state otherwise."""
        if entity_id == "predbat.status":
            return "£" * 50 if attribute is None else "£" * 80 if attribute == "detail" else default
        return real(entity_id, default, attribute)

    gw.get_state_wrapper = states
    with clock_at(gw, NOW):
        asyncio.run(gw._publish_predbat_data())
    payload = json.loads(gw._published[0][1])
    assert len(payload["predbat_status"].encode("utf-8")) <= 31
    assert len(payload["predbat_status_detail"].encode("utf-8")) <= 63
    assert payload["predbat_status"] == "£" * 15


def test_unsent_predbat_data_is_not_deduplicated():
    """A publish dropped while disconnected must go out again once connected."""
    gw = published_gateway()
    sent = []
    connected = [False]

    async def raw(topic, payload, retain=False):
        """Drop the publish while disconnected, as _publish_raw does."""
        if not connected[0]:
            return False
        sent.append(payload)
        return True

    gw._publish_raw = raw
    with clock_at(gw, NOW):
        asyncio.run(gw._publish_predbat_data())
        assert gw._last_predbat_data is None and not sent
        connected[0] = True
        asyncio.run(gw._publish_predbat_data())
    assert len(sent) == 1 and gw._last_predbat_data is not None


def test_oversized_packet_warns_at_most_every_15_minutes():
    """A packet that stays over 1400 bytes is not re-logged every cycle."""
    gw = published_gateway()
    gw._topic_base = "predbat/devices/" + DEVICE + "/" + "x" * 1400

    def warnings():
        """Count the oversize warnings logged so far."""
        return sum("exceeds 1400-byte limit" in str(c.args[0]) for c in gw.log.call_args_list if c.args)

    for offset in (0, 60, 120):
        with clock_at(gw, NOW + offset):
            asyncio.run(gw._publish_predbat_data())
    assert not gw._published and warnings() == 1
    with clock_at(gw, NOW + 15 * 60):
        asyncio.run(gw._publish_predbat_data())
    assert warnings() == 2


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
    gw.component_loop = None
    gw._last_live_binding = None
    gw._last_live_binding_publish = 0
    with clock_at(gw, NOW):
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
    test_screen_off_revokes_the_live_binding()
    test_status_strings_fit_the_firmware_buffers_in_bytes()
    test_unsent_predbat_data_is_not_deduplicated()
    test_oversized_packet_warns_at_most_every_15_minutes()


if __name__ == "__main__":
    import sys

    write_wire_fixture(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
