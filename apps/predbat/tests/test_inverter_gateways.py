"""Actual provider config and mutation boundaries with synthetic route ownership."""

import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import gateway_status_pb2 as pb
import pytest

from control_handoff import ControlWriteBlocked, ControlWriteGate
from gateway import GatewayMQTT
from gecloud import GE_API_INVERTER_WRITE_SETTING
from inverter_gateways import GatewayEndpoint, InverterGatewayRuntime, InverterRoute, physical_identity
from mock_base import MockBase
from tests.test_control_handoff import FakeSession, make_cloud

A = "CE12345678"
B = "FD98765432"


ENDPOINT_IDS = {"givenergy": "11111111-1111-4111-8111-111111111111", "predbat_gateway": "22222222-2222-4222-8222-222222222222"}
ENDPOINTS = tuple(GatewayEndpoint(endpoint, provider, "33333333-3333-4333-8333-333333333333") for provider, endpoint in ENDPOINT_IDS.items())


def route(manufacturer, serial, primary, secondary=()):
    """Resolve synthetic inventory IDs independently of transport/provider names."""
    return InverterRoute(manufacturer, serial, ENDPOINT_IDS[primary], tuple(ENDPOINT_IDS[provider] for provider in secondary))


def make_base(routes, gates=True):
    """Make one shared optimiser and independently owned synthetic control epochs."""
    base = MockBase()
    base.args = {"ge_cloud_direct": True, "gateway": True, "unrelated": "preserved"}
    base.log = lambda *_args, **_kwargs: None
    owners = {route.identity: ControlWriteGate(next(endpoint.provider for endpoint in ENDPOINTS if endpoint.endpoint_id == route.primary), "epoch-" + route.serial, 7, time.monotonic() + 60) for route in routes} if gates else {}
    runtime = InverterGatewayRuntime(base, tuple(routes), tuple(endpoint for endpoint in ENDPOINTS if any(endpoint.endpoint_id in (route.primary,) + route.secondary for route in routes)), owners)
    base.__inverter_gateway_runtime = runtime
    return base, runtime, owners


def cloud_for(base):
    """Exercise the real Cloud automatic configurer and HTTP transport."""
    cloud = make_cloud()
    cloud.base = base
    cloud.prefix = "predbat"
    cloud.info = {}
    cloud.site_export_limit = None
    cloud.ems_disabled_ge_cloud_data = False
    names = ["Battery Charge Power", "Battery Discharge Power", "Battery Reserve Percent", "AC Charge Upper Percent Limit", "Enable Eco Mode"]
    cloud.settings = {serial.lower(): {str(index): {"name": name} for index, name in enumerate(names)} for serial in (A, B)}
    return cloud


def devices(*serials):
    """The actual Cloud convention is lowercase full serials."""
    return {"ems": None, "gateway": None, "battery": [serial.lower() for serial in serials], "pv": []}


def hub_for(base, serials):
    """Use actual Hub component initialization and protobuf discovery."""
    hub = GatewayMQTT.__new__(GatewayMQTT)
    hub.base = base
    hub.prefix = "predbat"
    hub.args = {}
    hub.log = lambda *_args, **_kwargs: None
    hub.initialize(gateway_device_id="pbgw_test", mqtt_host="synthetic.invalid", gateway_shared_ct=False)
    hub._mqtt_client = SimpleNamespace(publish=AsyncMock())
    hub._mqtt_connected = True
    hub._last_status = pb.GatewayStatus(device_id="pbgw_test")
    for serial in serials:
        unit = hub._last_status.inverters.add(serial=serial, type=pb.INVERTER_TYPE_GIVENERGY, primary=True, connected=True, active=True)
        unit.battery.capacity_wh = 9500
        unit.battery.soc_percent = 50
    return hub


def configure(base, serials_cloud, serials_hub):
    """Call both actual auto-configurers, not reconstructed entity fixtures."""
    cloud, hub = cloud_for(base), hub_for(base, serials_hub)
    asyncio.run(cloud.async_automatic_config(devices(*serials_cloud)))
    hub.automatic_config()
    return cloud, hub


def test_physical_identity_deduplicates_gateway_case_only():
    """Provider-specific canonicalisation does not accept suffix or other vendors."""
    assert physical_identity("givenergy", A.lower()) == ("givenergy", A)
    with pytest.raises(ValueError):
        InverterGatewayRuntime(MockBase(), (route("givenergy", A, "givenergy"), route("givenergy", A.lower(), "predbat_gateway")), ENDPOINTS)
    for manufacturer, serial in (("foxess", A), ("givenergy", "5678"), ("givenergy", " " + A)):
        with pytest.raises(ValueError):
            physical_identity(manufacturer, serial)


def test_dual_gateway_single_inverter_does_not_duplicate_or_disable_cloud():
    """Cloud and Hub stay configured while only one physical slot is composed."""
    base, _runtime, _owners = make_base([route("givenergy", A, "givenergy", ("predbat_gateway",))])
    cloud = cloud_for(base)
    hub = hub_for(base, [A])
    hub.automatic_config()
    assert "num_inverters" not in base.args
    asyncio.run(cloud.async_automatic_config(devices(A)))
    expected = copy.deepcopy(base.args)
    assert expected["num_inverters"] == 1
    assert expected["inverter_type"] == ["GEC"]
    assert A.lower() in expected["soc_percent"][0]
    assert expected["ge_cloud_direct"] is True
    assert expected["gateway"] is True
    hub.automatic_config()
    assert base.args == expected


def test_two_physical_inverters_route_independently_of_discovery_order():
    """Actual mixed entity mappings route one inverter to each selected gateway."""
    routes = [route("givenergy", B, "predbat_gateway", ("givenergy",)), route("givenergy", A, "givenergy", ("predbat_gateway",))]
    base, _runtime, _owners = make_base(routes)
    configure(base, [B, A], [B, A])
    assert base.args["num_inverters"] == 2
    assert base.args["inverter_type"] == ["GEC", "GWMQTT"]
    assert A.lower() in base.args["charge_rate"][0]
    assert B[-6:].lower() in base.args["charge_rate"][1]
    assert base.args["ge_cloud_direct"] is True
    assert base.args["unrelated"] == "preserved"


def test_failed_secondary_discovery_does_not_close_primary_cloud_gate():
    """Standby absence is not automatic takeover or primary control failure."""
    base, runtime, owners = make_base([route("givenergy", A, "givenergy", ("predbat_gateway",))])
    cloud, hub = configure(base, [A], [A])
    original = copy.deepcopy(base.args)
    hub._last_status = None
    with pytest.raises(ValueError):
        hub.automatic_config()
    assert runtime.gate_for("givenergy", A) is owners[("givenergy", A)]
    assert not owners[("givenergy", A)].snapshot()["closed"]
    assert base.args == original
    assert cloud.base is hub.base


def test_bad_selected_discovery_seals_without_using_secondary():
    """Missing, extra or different identities never change effective primary."""
    base, runtime, owners = make_base([route("givenergy", A, "givenergy", ("predbat_gateway",))])
    cloud, _hub = configure(base, [A], [A])
    original = copy.deepcopy(base.args)
    with pytest.raises(ValueError):
        asyncio.run(cloud.async_automatic_config(devices(B)))
    assert owners[("givenergy", A)].snapshot()["closed"]
    assert base.args == original
    for provider in ("givenergy", "predbat_gateway"):
        with pytest.raises(ControlWriteBlocked):
            runtime.gate_for(provider, A)


def test_changed_mapping_requires_new_boot_and_keeps_published_config():
    """Rediscovery cannot silently replace an admitted slot's control entities."""
    base, _runtime, owners = make_base([route("givenergy", A, "givenergy")])
    cloud = cloud_for(base)
    asyncio.run(cloud.async_automatic_config(devices(A)))
    original = copy.deepcopy(base.args)
    cloud.prefix = "another"
    with pytest.raises(ValueError):
        asyncio.run(cloud.async_automatic_config(devices(A)))
    assert base.args == original
    assert owners[("givenergy", A)].snapshot()["closed"]


@pytest.mark.parametrize("aggregate", ["ems", "gateway", "pv"])
def test_aggregate_and_pv_cloud_scope_rejected_before_config_changes(aggregate):
    """A full serial does not turn a whole-plant actuator into an independent unit."""
    routes = [route("givenergy", A, "givenergy"), route("givenergy", B, "givenergy")]
    base, _runtime, owners = make_base(routes)
    cloud = cloud_for(base)
    original = copy.deepcopy(base.args)
    discovery = devices(A, B)
    discovery[aggregate] = ["PV123456"] if aggregate == "pv" else "EMS123456"
    with pytest.raises(ValueError):
        asyncio.run(cloud.async_automatic_config(discovery))
    assert base.args == original
    assert all(gate.snapshot()["closed"] for gate in owners.values())


def test_hub_suffix_collision_is_not_a_full_identity_match():
    """Entity names cannot safely distinguish two identical serial suffixes."""
    other = "XX12345678"
    base, _runtime, owners = make_base([route("givenergy", A, "predbat_gateway"), route("givenergy", other, "predbat_gateway")])
    with pytest.raises(ValueError, match="suffix collision"):
        hub_for(base, [A, other]).automatic_config()
    assert all(gate.snapshot()["closed"] for gate in owners.values())


def test_missing_grant_stays_read_only_even_after_real_discovery():
    """A composed mapping supplies no write authority by itself."""
    base, runtime, _owners = make_base([route("givenergy", A, "givenergy", ("predbat_gateway",))], gates=False)
    configure(base, [A], [A])
    assert base.args["num_inverters"] == 1
    with pytest.raises(ControlWriteBlocked):
        runtime.gate_for("givenergy", A)


def test_actual_cloud_sink_uses_exact_primary_and_retains_ambiguity():
    """HTTP POST attempts are tracked per physical inverter and blocked after close."""
    base, _runtime, owners = make_base([route("givenergy", A, "givenergy", ("predbat_gateway",)), route("givenergy", B, "predbat_gateway", ("givenergy",))])
    cloud, _hub = configure(base, [A, B], [A, B])

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def outcome():
            """Hold an actual submitted provider call across closure."""
            started.set()
            await release.wait()
            raise asyncio.TimeoutError()

        session = FakeSession(outcome)
        with patch("gecloud.aiohttp.ClientSession", return_value=session):
            for serial in (B, A[-6:]):
                with pytest.raises((ControlWriteBlocked, ValueError)):
                    await cloud.async_get_inverter_data(GE_API_INVERTER_WRITE_SETTING, serial, 1, post=1)
            gate = owners[("givenergy", A)]
            task = asyncio.create_task(cloud.async_get_inverter_data(GE_API_INVERTER_WRITE_SETTING, A.lower(), 1, post=1))
            await started.wait()
            assert gate.snapshot()["pending"] == 1
            gate.close(gate.epoch, gate.config_version, "synthetic-transition")
            release.set()
            assert await task is None
            assert gate.snapshot()["uncertain"] == 1
            with pytest.raises(ControlWriteBlocked):
                await cloud.async_get_inverter_data(GE_API_INVERTER_WRITE_SETTING, A, 1, post=1)
        assert len(session.requests) == 1

    asyncio.run(exercise())


def test_actual_hub_sink_blocks_secondary_global_and_retained_schedule():
    """Selected command reaches MQTT once, battery admission leaves EV/display alone."""
    base, _runtime, owners = make_base([route("givenergy", A, "givenergy", ("predbat_gateway",)), route("givenergy", B, "predbat_gateway", ("givenergy",))])
    _cloud, hub = configure(base, [A, B], [A, B])

    async def exercise():
        for command in ({"command": "set_charge_rate", "serial": A}, {"command": "set_read_only", "serial": B}, {"command": "set_charge_rate"}, {"command": "set_charge_rate", "serial": B[-6:]}):
            with pytest.raises((ControlWriteBlocked, ValueError)):
                await hub._publish_raw(hub.topic_command, json.dumps(command).encode())
        with pytest.raises(ControlWriteBlocked):
            await hub._publish_raw(hub.topic_schedule, b"retained-executable-plan", retain=True)
        await hub.publish_command("set_charge_rate", serial=B, power_w=1000)
        assert hub._mqtt_client.publish.await_count == 1
        assert owners[("givenergy", B)].snapshot()["uncertain"] == 1
        owners[("givenergy", B)].seal()
        with pytest.raises(ControlWriteBlocked):
            await hub.publish_command("set_charge_rate", serial=B, power_w=2000)
        await hub._publish_raw(hub.topic_ev_command, b"EV")
        await hub._publish_raw(hub._topic_base + "/predbat_data", b"display")
        assert hub._mqtt_client.publish.await_count == 3

    asyncio.run(exercise())


def test_single_provider_local_order_does_not_bind_global_rate_cap_slot():
    """Hub-local slot zero may be global slot one when another gateway owns slot zero."""
    base, _runtime, _owners = make_base([route("givenergy", A, "givenergy"), route("givenergy", B, "predbat_gateway")])
    _cloud, hub = configure(base, [A], [B])
    base.inverters = [SimpleNamespace(id=0, inverter_type="GEC", battery_rate_max_charge=1, battery_rate_max_discharge=1), SimpleNamespace(id=1, inverter_type="GWMQTT", battery_rate_max_charge=2, battery_rate_max_discharge=3)]
    assert hub._inverter_slot_serials == [B]
    assert hub._inverter_rate_caps() == {B: (120000, 180000)}


def test_background_paths_do_not_release_secondary_or_queue_executable_plan():
    """Actual housekeeping cannot undo coordinator-owned standby fencing."""
    base, _runtime, _owners = make_base([route("givenergy", A, "givenergy", ("predbat_gateway",))])
    cloud, hub = configure(base, [A], [A])
    hub_primary_base, _runtime2, _owners2 = make_base([route("givenergy", A, "predbat_gateway", ("givenergy",))])
    standby_cloud, _hub2 = configure(hub_primary_base, [A], [A])

    async def exercise():
        hub._pending_plan = None
        await hub._check_read_only_state()
        await hub._check_inverter_resets()
        await hub.publish_plan([], "Europe/London")
        hub._last_plan_entries = [{"irrelevant": "cached prior plan"}]
        hub._last_plan_timezone = "Europe/London"
        hub._last_plan_publish_time = 0
        await hub._republish_plan_if_stale()
        assert hub._pending_plan is None
        hub._mqtt_client.publish.assert_not_awaited()
        # Cloud defaults for a standby unit skip before inspecting or writing registers.
        with patch.object(standby_cloud, "async_write_inverter_setting", new=AsyncMock()) as write:
            await standby_cloud.enable_default_options(A.lower(), {"1": {"name": "Force Charge"}})
            write.assert_not_awaited()

    asyncio.run(exercise())


def test_successful_actual_cloud_write_does_not_latch_uncertainty():
    """Provider completion classification remains the actual documented contract."""
    base, _runtime, owners = make_base([route("givenergy", A, "givenergy")])
    cloud = cloud_for(base)
    asyncio.run(cloud.async_automatic_config(devices(A)))

    async def exercise():
        async def outcome():
            """Return the documented single-device successful resulting value."""
            return {"data": {"success": True, "value": 1000}}

        with patch("gecloud.aiohttp.ClientSession", return_value=FakeSession(outcome)):
            assert await cloud.async_get_inverter_data(GE_API_INVERTER_WRITE_SETTING, A.lower(), 1, post=True) == {"success": True, "value": 1000}
        assert owners[("givenergy", A)].snapshot()["pending"] == 0
        assert owners[("givenergy", A)].snapshot()["uncertain"] == 0

    asyncio.run(exercise())


def test_gate_expiry_never_selects_secondary_or_closes_another_inverter():
    """Per-inverter expiry keeps one authority and cannot cascade to a separate unit."""
    routes = [route("givenergy", A, "givenergy", ("predbat_gateway",)), route("givenergy", B, "predbat_gateway", ("givenergy",))]
    base, runtime, owners = make_base(routes)
    configure(base, [A, B], [A, B])
    expired = owners[("givenergy", A)]
    expired.expires_at = time.monotonic() - 1
    with pytest.raises(ControlWriteBlocked):
        runtime.gate_for("givenergy", A).admit("givenergy")
    with pytest.raises(ControlWriteBlocked):
        runtime.gate_for("predbat_gateway", A)
    assert not owners[("givenergy", B)].snapshot()["closed"]


def test_runtime_is_excluded_from_actual_debug_export():
    """Locks, gateway inventory and private future coordinators do not break support export."""
    import sys
    import yaml

    with patch.object(sys, "frozen", True, create=True):
        __import__("predbat")
    from userinterface import UserInterface

    base, _runtime, _owners = make_base([route("givenergy", A, "givenergy")])
    base.args = {}
    base.CONFIG_ITEMS = []
    base.inverters = []
    base.config_root_p = base.config_root
    base.minutes_now = 42
    text = UserInterface.create_debug_yaml(base, write_file=False)
    assert "__inverter_gateway_runtime" not in yaml.safe_load(text)
    assert "python/object" not in text


def test_actual_hub_pending_publish_survives_cancel_without_draining_or_affecting_cloud():
    """An in-flight owner-loop future stays owned by its exact physical inverter."""
    base, runtime, owners = make_base([route("givenergy", A, "givenergy", ("predbat_gateway",)), route("givenergy", B, "predbat_gateway", ("givenergy",))])
    _cloud, hub = configure(base, [A, B], [A, B])

    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()

        async def publish(*_args, **_kwargs):
            """Hold the actual MQTT sink across caller cancellation and closure."""
            started.set()
            await release.wait()

        hub._mqtt_client.publish = AsyncMock(side_effect=publish)
        hub._loop = asyncio.get_running_loop()
        task = asyncio.create_task(hub.publish_command("set_charge_rate", serial=B, power_w=1000))
        await started.wait()
        gate = owners[("givenergy", B)]
        gate.close(gate.epoch, gate.config_version, "synthetic-transition")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gate.snapshot()["pending"] == 1
        assert not gate.wait_drained(gate.epoch, gate.config_version, "synthetic-transition", 0)
        assert not owners[("givenergy", A)].snapshot()["closed"]
        assert runtime.gate_for("givenergy", A) is owners[("givenergy", A)]
        release.set()
        for _attempt in range(10):
            await asyncio.sleep(0)
            if gate.snapshot()["pending"] == 0:
                break
        assert gate.snapshot()["pending"] == 0
        assert gate.snapshot()["uncertain"] == 1
        assert not gate.wait_drained(gate.epoch, gate.config_version, "synthetic-transition", 0)

    asyncio.run(exercise())
