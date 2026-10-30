"""Exercise Demo cleanup against registers independent of the desired entity cache."""

import asyncio
from types import MethodType, SimpleNamespace

from execute import Execute
from solis import SolisAPI, SOLIS_READ_ENDPOINT, SOLIS_READ_BATCH_ENDPOINT, SOLIS_CONTROL_ENDPOINT
from tests.test_solis import MockSolisAPI, _make_run_api, _FakeAiohttpSession
from unittest.mock import patch
import solis as solis_module


def make_handover_api(v2=True, fields=12):
    """Use real CID read/write verification with a separate fake physical device."""
    api = MockSolisAPI()
    sn = "TEST001"
    api.inverter_sn = [sn]
    api.base.inverter_needs_reset_force = "set_read_only"
    api.dashboard_items["switch.predbat_set_read_only"] = {"state": "on", "attributes": {}}
    slot = ["100", "80", "23:30-05:30", "01:00-04:00"] if fields == 12 else ["100", "80", "23:30", "05:30", "01:00", "04:00"]
    api.hardware = {6798: "43605" if v2 else "21845", 636: "114", 103: ",".join(slot * 3), 157: "55", 158: "12", 7229: "15"}
    api.hardware.update({cid: "1" for cid in range(5916, 5928)})
    api.hardware_writes = []
    api.refuse_cid = None
    api.missing_cid = None
    api.block_writes = None
    # Desired state already says off, reproducing the false local acknowledgement.
    api.charge_discharge_time_windows[sn] = {1: {"charge_enable": 0, "discharge_enable": 0}}
    api.cached_values[sn] = {cid: "0" for cid in range(5916, 5928)}

    async def transport(endpoint, payload):
        """Return device registers, including successful acknowledgements of refused writes."""
        cid = payload.get("cid")
        if endpoint == SOLIS_READ_ENDPOINT:
            return {"msg": api.hardware[cid]}
        if endpoint == SOLIS_READ_BATCH_ENDPOINT:
            return [[{"cid": cid, "msg": api.hardware[cid]} for cid in map(int, payload["cids"].split(",")) if cid != api.missing_cid]]
        assert endpoint == SOLIS_CONTROL_ENDPOINT, endpoint
        if api.block_writes is not None:
            await api.block_writes.wait()
        api.hardware_writes.append((cid, payload["value"]))
        if cid != api.refuse_cid:
            api.hardware[cid] = payload["value"]
        return [{"code": "0"}]

    async def publish():
        """Avoid unrelated dashboard plumbing in a transport test."""

    api._execute_request = transport
    api.read_and_write_cid = MethodType(SolisAPI.read_and_write_cid, api)
    api.publish_entities = publish
    return api


async def test_verified_cleanup():
    """Clear both TOU formats using actual readbacks, including cached-off registers."""
    for v2, fields in [(True, 12), (False, 12), (False, 18)]:
        api = make_handover_api(v2, fields)
        assert await api.perform_read_only_reset()
        assert api.hardware[636] == "49", "Preserve backup/grid permissions while leaving feed-in and timed mode"
        assert [api.hardware[cid] for cid in [157, 158, 7229]] == ["55", "12", "15"], "Protection limits must not change"
        if v2:
            assert all(api.hardware[cid] == "0" for cid in range(5916, 5928))
            assert api.charge_discharge_time_windows["TEST001"][1]["discharge_enable"] == 0
            assert {cid for cid, _value in api.hardware_writes} == set(range(5916, 5928)) | {636}
        else:
            assert "23:30" not in api.hardware[103] and "01:00" not in api.hardware[103]
            assert set(api.hardware[103].split(",")) <= {"0", "00:00", "00:00-00:00"}
            assert {cid for cid, _value in api.hardware_writes} == {103, 636}


async def test_refusal_and_missing_data():
    """Neither an API acknowledgement nor missing registers proves a schedule cleared."""
    api = make_handover_api()
    api.refuse_cid = 5922
    assert not await api.perform_read_only_reset()
    assert api.hardware[5922] == "1"
    assert "not confirmed" in api.read_only_reset_error
    api = make_handover_api()
    api.missing_cid = 5927
    assert not await api.perform_read_only_reset()
    assert not api.hardware_writes, "An incomplete initial read must not trigger cleanup writes"


async def test_explicit_transition_only():
    """Already-Demo startup and passive components must preserve a manual schedule."""
    for control, force, state in [(False, "set_read_only", "on"), (True, "", "on"), (True, "set_read_only", "off")]:
        api = make_handover_api()
        api.control_enable = control
        api.base.inverter_needs_reset_force = force
        api.dashboard_items["switch.predbat_set_read_only"]["state"] = state
        assert not await api.perform_read_only_reset()
        assert not api.hardware_writes
    api = _make_run_api(configured_sns=["TEST001"])
    api.dashboard_items["switch.predbat_set_read_only"] = {"state": "on", "attributes": {}}

    async def discover():
        """Provide a reachable battery inverter to the real startup path."""
        return [{"sn": "TEST001"}]

    api.get_inverter_list = discover
    with patch.object(solis_module.aiohttp, "ClientSession", return_value=_FakeAiohttpSession()):
        await api.run(0, True)
    assert not api.startup_reset_registers_calls and not api.write_time_windows_calls


async def test_cross_thread_ack_and_timeout():
    """The synchronous executor waits for hardware completion and cancels expired work."""
    api = make_handover_api()
    api.control_event_loop = asyncio.get_running_loop()
    assert await asyncio.to_thread(api.request_read_only_reset)
    assert api.hardware[5922] == "0"
    api = make_handover_api()
    api.control_event_loop = asyncio.get_running_loop()
    api.block_writes = asyncio.Event()
    assert not await asyncio.to_thread(api.request_read_only_reset, 0.01)
    await asyncio.sleep(0)
    api.block_writes.set()
    await asyncio.sleep(0.01)
    assert not api.hardware_writes, "A timed-out request must not dispatch writes later"


async def test_handover_waits_for_polling():
    """A stale poll or earlier writer must finish before cleanup can confirm release."""
    api = make_handover_api()
    poll_started = asyncio.Event()
    poll_release = asyncio.Event()

    async def poll_cycle(seconds, first):
        """Model a poll returning the old enabled state after the Demo request arrives."""
        poll_started.set()
        await poll_release.wait()
        api.cached_values["TEST001"][5922] = "1"
        return True

    api.run_cycle = poll_cycle
    polling = asyncio.create_task(api.run(60, False))
    await poll_started.wait()
    handover = asyncio.create_task(asyncio.to_thread(api.request_read_only_reset))
    await asyncio.sleep(0.01)
    assert not handover.done() and not api.hardware_writes
    poll_release.set()
    await polling
    assert await handover
    assert api.hardware[5922] == "0" and api.cached_values["TEST001"][5922] == "0"


def test_executor_handover():
    """Force reset bypasses local acknowledgements and surfaces a failed physical cleanup."""
    for success in (True, False):
        calls = []
        component = SimpleNamespace(read_only_reset_error="" if success else "device refused cleanup")

        def request():
            """Observe the force marker while the executor waits for cleanup."""
            calls.append(executor.inverter_needs_reset_force)
            return success

        component.request_read_only_reset = request
        executor = SimpleNamespace(
            set_read_only=True,
            inverter_needs_reset=True,
            inverter_needs_reset_force="set_read_only",
            inverters=[SimpleNamespace(inverter_type="SolisCloud")],
            components=SimpleNamespace(get_component=lambda name: component if name == "solis" else None),
            record_status=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
        executor.solis_read_only_component = MethodType(Execute.solis_read_only_component, executor)
        Execute.reset_inverter(executor)
        assert calls[0] == "set_read_only"
        assert not executor.inverter_needs_reset and executor.inverter_needs_reset_force == ""
        if not success:
            assert calls[1][1]["had_errors"]
            executor.alert_active_keep = executor.manual_soc_keep = executor.manual_soc_max_keep = {}
            executor.minutes_now = 0
            executor.set_charge_freeze_only = False
            executor.holiday_days_left = 0
            executor.clear_control_ledger = lambda reason: None
            assert Execute.execute_plan(executor) == ("Warn: Solis handover unconfirmed", "device refused cleanup")
        calls.clear()
        Execute.reset_inverter(executor)
        assert not calls, "An existing Demo session must not clear later manual schedules"


def run_solis_handover_tests():
    """Run the bounded hardware-readback and executor integration regressions."""
    for test in [test_verified_cleanup, test_refusal_and_missing_data, test_explicit_transition_only, test_cross_thread_ack_and_timeout, test_handover_waits_for_polling]:
        asyncio.run(test())
    test_executor_handover()
    print("PASSED: Solis Demo handover readback, refusal, timeout and executor regressions")
    return False
