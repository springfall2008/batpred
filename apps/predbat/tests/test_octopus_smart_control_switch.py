# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from octopus import OctopusAPI

ACTIVE_ID = "smart-charge-1001"
SUSPENDED_ID = "smart-charge-2002"


def _check(name, condition, detail=""):
    """
    Print an error for a failed condition and return whether it failed.
    """
    if not condition:
        print("ERROR: {} {}".format(name, detail))
        return True
    return False


def _api(my_predbat):
    """
    An OctopusAPI with an active and a suspended device, recording what it publishes and the args it sets.
    """
    api = OctopusAPI(my_predbat, key="", account_id="acc-1", automatic=False)
    api.intelligent_devices = {
        ACTIVE_ID: {"suspended": False, "planned_dispatches": [], "completed_dispatches": []},
        SUSPENDED_ID: {"suspended": True, "planned_dispatches": [], "completed_dispatches": []},
    }
    api.published = {}
    api.dashboard_item = lambda entity, state, attributes=None, app=None: api.published.__setitem__(entity, state)
    api.args_set = {}
    api.set_arg = lambda name, value: api.args_set.__setitem__(name, value)
    return api


def _switch(api, device_id):
    """
    The Smart Control switch entity of a device.
    """
    return api.get_entity_name("switch", "intelligent_smart_charge", index=api.device_id_to_index_suffix(device_id))


async def _run(my_predbat):
    """
    The Smart Control switch of the built-in Octopus component: state, both commands, rollback and wiring.
    """
    failed = False
    api = _api(my_predbat)
    active_switch = _switch(api, ACTIVE_ID)
    suspended_switch = _switch(api, SUSPENDED_ID)

    print("Test 1: the switch is published for every device, on while not suspended - suspended devices included")
    await api.async_intelligent_update_sensor("acc-1")
    failed |= _check("t1 active on", api.published.get(active_switch) == "on", "published {}".format(api.published))
    failed |= _check("t1 suspended off", api.published.get(suspended_switch) == "off", "published {}".format(api.published))

    print("Test 2: turning the switch off shows off straight away and queues a SUSPEND command")
    await api.switch_event(active_switch, "turn_off")
    failed |= _check("t2 state", api.published.get(active_switch) == "off", "published {}".format(api.published))
    failed |= _check("t2 cached", api.intelligent_devices[ACTIVE_ID]["suspended"] is True, "")
    failed |= _check("t2 queued", [c["command"] for c in api.commands] == ["set_intelligent_smart_control"] and api.commands[0]["value"] is False and api.commands[0]["device_id"] == ACTIVE_ID, "commands {}".format(api.commands))
    api.async_graphql_query = AsyncMock(return_value={"updateDeviceSmartControl": {"id": ACTIVE_ID}})
    failed |= _check("t2 processed", await api.process_commands("acc-1") is True, "")
    mutation = api.async_graphql_query.call_args[0][0]
    failed |= _check("t2 mutation", "updateDeviceSmartControl" in mutation and "action: SUSPEND" in mutation and ACTIVE_ID in mutation, "mutation {}".format(mutation))
    failed |= _check("t2 stays off", api.published.get(active_switch) == "off", "")

    print("Test 3: turning it back on sends UNSUSPEND, also for a device that is suspended")
    api.commands = []
    api.smart_control_confirmed = {}
    await api.switch_event(suspended_switch, "turn_on")
    failed |= _check("t3 state", api.published.get(suspended_switch) == "on", "published {}".format(api.published))
    api.async_graphql_query = AsyncMock(return_value={"updateDeviceSmartControl": {"id": SUSPENDED_ID}})
    await api.process_commands("acc-1")
    mutation = api.async_graphql_query.call_args[0][0]
    failed |= _check("t3 mutation", "action: UNSUSPEND" in mutation and SUSPENDED_ID in mutation, "mutation {}".format(mutation))
    failed |= _check("t3 cached", api.intelligent_devices[SUSPENDED_ID]["suspended"] is False, "")

    print("Test 4: toggle flips the state")
    api.commands = []
    api.smart_control_confirmed = {}
    await api.switch_event(active_switch, "toggle")
    failed |= _check("t4 toggled on", api.published.get(active_switch) == "on" and api.commands[0]["value"] is True, "commands {}".format(api.commands))
    api.commands = []
    api.smart_control_confirmed = {}

    print("Test 5: a failed command puts the switch back to how it was")
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False
    await api.switch_event(active_switch, "turn_off")
    failed |= _check("t5 shows off first", api.published.get(active_switch) == "off", "")
    api.async_graphql_query = AsyncMock(return_value=None)
    await api.process_commands("acc-1")
    failed |= _check("t5 rolled back", api.published.get(active_switch) == "on" and api.intelligent_devices[ACTIVE_ID]["suspended"] is False, "published {}".format(api.published))

    print("Test 5b: an exception or an empty reply also puts the switch back")
    for reply in (RuntimeError("boom"), {"updateDeviceSmartControl": None}):
        api.commands = []
        api.smart_control_confirmed = {}
        api.intelligent_devices[ACTIVE_ID]["suspended"] = False
        await api.switch_event(active_switch, "turn_off")
        api.async_graphql_query = AsyncMock(side_effect=reply) if isinstance(reply, Exception) else AsyncMock(return_value=reply)
        await api.process_commands("acc-1")
        failed |= _check("t5b rolled back {}".format(type(reply).__name__), api.published.get(active_switch) == "on" and api.intelligent_devices[ACTIVE_ID]["suspended"] is False, "published {}".format(api.published))

    print("Test 5c: a poll straight after a successful change does not flip the switch back until Octopus reports it")
    api.commands = []
    api.smart_control_confirmed = {}
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False
    await api.switch_event(active_switch, "turn_off")
    api.async_graphql_query = AsyncMock(return_value={"updateDeviceSmartControl": {"id": ACTIVE_ID}})
    await api.process_commands("acc-1")
    polled = {"suspended": False}
    api.apply_smart_control_pending(ACTIVE_ID, polled)
    failed |= _check("t5c override held", polled["suspended"] is True, "polled {}".format(polled))
    polled = {"suspended": True}
    api.apply_smart_control_pending(ACTIVE_ID, polled)
    failed |= _check("t5c cleared once reported", ACTIVE_ID not in api.smart_control_pending and polled["suspended"] is True, "pending {}".format(api.smart_control_pending))
    api.smart_control_pending[ACTIVE_ID] = (True, datetime.now() - timedelta(seconds=1))
    polled = {"suspended": False}
    api.apply_smart_control_pending(ACTIVE_ID, polled)
    failed |= _check("t5c expires", polled["suspended"] is False and ACTIVE_ID not in api.smart_control_pending, "polled {}".format(polled))
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False

    print("Test 5d: devices whose ids end alike are told apart, and the name says which car")
    other_id = "other-9" + api.device_id_to_index_suffix(ACTIVE_ID)
    api.intelligent_devices[other_id] = {"suspended": False, "model": "iX3", "planned_dispatches": [], "completed_dispatches": []}
    api.commands = []
    api.smart_control_confirmed = {}
    await api.switch_event(api.get_entity_name("switch", "intelligent_smart_charge", index=api.device_id_to_index_suffix(ACTIVE_ID)), "turn_off")
    failed |= _check("t5d right device", [c["device_id"] for c in api.commands] == [ACTIVE_ID] and api.intelligent_devices[other_id]["suspended"] is False, "commands {}".format(api.commands))
    api.intelligent_devices.pop(other_id)
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False
    api.intelligent_devices[ACTIVE_ID]["model"] = "iX3"
    captured = {}
    api.dashboard_item = lambda entity, state, attributes=None, app=None: captured.__setitem__(entity, attributes)
    api.publish_smart_control_switch(ACTIVE_ID, api.intelligent_devices[ACTIVE_ID])
    failed |= _check("t5d name", "iX3" in captured[active_switch]["friendly_name"], "captured {}".format(captured))
    api.dashboard_item = lambda entity, state, attributes=None, app=None: api.published.__setitem__(entity, state)
    api.commands = []
    api.smart_control_confirmed = {}

    print("Test 5e: two queued changes become one, and when it fails the switch goes back to what Octopus confirmed")
    api.commands = []
    api.smart_control_confirmed = {}
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False
    await api.switch_event(active_switch, "turn_off")
    await api.switch_event(active_switch, "turn_on")
    failed |= _check("t5e coalesced", [(c["device_id"], c["value"]) for c in api.commands] == [(ACTIVE_ID, True)], "commands {}".format(api.commands))
    api.async_graphql_query = AsyncMock(return_value=None)
    await api.process_commands("acc-1")
    failed |= _check("t5e confirmed state", api.published.get(active_switch) == "on" and api.intelligent_devices[ACTIVE_ID]["suspended"] is False, "published {}".format(api.published))
    failed |= _check("t5e baseline dropped", ACTIVE_ID not in api.smart_control_confirmed, "confirmed {}".format(api.smart_control_confirmed))

    print("Test 5f: a change made while another is being sent stays on show, and both failing restores what Octopus confirmed")
    api.commands = []
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False
    await api.switch_event(active_switch, "turn_off")

    async def _fail_and_switch_back_on(query, *args, **kwargs):
        """
        Fail the mutation, with the user turning the switch back on while it was being sent.
        """
        if "action: SUSPEND" in query:
            await api.switch_event(active_switch, "turn_on")
        return None

    api.async_graphql_query = AsyncMock(side_effect=_fail_and_switch_back_on)
    await api.process_commands("acc-1")
    failed |= _check("t5f newer change shown", api.published.get(active_switch) == "on" and [c["value"] for c in api.commands] == [True], "published {} commands {}".format(api.published, api.commands))
    api.intelligent_devices[ACTIVE_ID]["suspended"] = True  # a stale display state must not become the rollback target
    await api.process_commands("acc-1")
    failed |= _check("t5f back to confirmed", api.published.get(active_switch) == "on" and api.intelligent_devices[ACTIVE_ID]["suspended"] is False, "published {}".format(api.published))

    print("Test 5g: the first change succeeding becomes the state a later failure goes back to")
    api.commands = []
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False
    await api.switch_event(active_switch, "turn_off")

    async def _succeed_then_fail(query, *args, **kwargs):
        """
        SUSPEND succeeds, with the user turning the switch back on while it was being sent; UNSUSPEND fails.
        """
        if "action: SUSPEND" in query:
            await api.switch_event(active_switch, "turn_on")
            return {"updateDeviceSmartControl": {"id": ACTIVE_ID}}
        return None

    api.async_graphql_query = AsyncMock(side_effect=_succeed_then_fail)
    await api.process_commands("acc-1")
    failed |= _check("t5g newer change shown", api.published.get(active_switch) == "on", "published {}".format(api.published))
    await api.process_commands("acc-1")
    failed |= _check("t5g back to the confirmed off", api.published.get(active_switch) == "off" and api.intelligent_devices[ACTIVE_ID]["suspended"] is True, "published {}".format(api.published))
    api.smart_control_pending = {}
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False

    print("Test 5h: a poll before a change is sent keeps the change on show, and becomes the state a failure goes back to")
    api.commands = []
    api.smart_control_confirmed = {}
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False
    await api.switch_event(active_switch, "turn_off")
    polled = {"suspended": False}
    api.apply_smart_control_pending(ACTIVE_ID, polled)
    failed |= _check("t5h change kept", polled["suspended"] is True and api.smart_control_confirmed.get(ACTIVE_ID) is False, "polled {} confirmed {}".format(polled, api.smart_control_confirmed))
    api.intelligent_devices[ACTIVE_ID] = dict(api.intelligent_devices[ACTIVE_ID], **polled)
    await api.switch_event(active_switch, "toggle")
    failed |= _check("t5h toggle reads the shown state", [c["value"] for c in api.commands] == [True], "commands {}".format(api.commands))
    await api.switch_event(active_switch, "turn_off")
    api.apply_smart_control_pending(ACTIVE_ID, {"suspended": True})
    failed |= _check("t5h poll updates confirmed", api.smart_control_confirmed.get(ACTIVE_ID) is True, "confirmed {}".format(api.smart_control_confirmed))
    api.async_graphql_query = AsyncMock(return_value=None)
    await api.process_commands("acc-1")
    failed |= _check("t5h fails back to the polled state", api.intelligent_devices[ACTIVE_ID]["suspended"] is True and ACTIVE_ID not in api.smart_control_confirmed, "device {}".format(api.intelligent_devices[ACTIVE_ID]))
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False

    print("Test 5i: a change for a device that has gone since does not leave its confirmed state behind")
    api.commands = []
    api.smart_control_confirmed = {}
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False
    await api.switch_event(active_switch, "turn_off")
    gone = api.intelligent_devices.pop(ACTIVE_ID)
    await api.process_commands("acc-1")
    failed |= _check("t5i baseline dropped", ACTIVE_ID not in api.smart_control_confirmed, "confirmed {}".format(api.smart_control_confirmed))
    api.intelligent_devices[ACTIVE_ID] = dict(gone, suspended=False)

    print("Test 5j: a successful change is held for longer than the poll straight after it")
    api.commands = []
    await api.switch_event(active_switch, "turn_off")
    api.async_graphql_query = AsyncMock(return_value={"updateDeviceSmartControl": {"id": ACTIVE_ID}})
    await api.process_commands("acc-1")
    expiry = api.smart_control_pending[ACTIVE_ID][1]
    failed |= _check("t5j held past the next 2-minute poll", expiry - datetime.now() > timedelta(minutes=4), "expiry {}".format(expiry))
    api.smart_control_pending = {}
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False

    print("Test 6: events for other entities, other services and unknown devices are ignored")
    api.commands = []
    api.smart_control_confirmed = {}
    await api.switch_event("switch.predbat_octopus_acc_1_something_else_1001", "turn_off")
    await api.switch_event(active_switch, "bogus")
    await api.switch_event(api.get_entity_name("switch", "intelligent_smart_charge", index="9999"), "turn_off")
    failed |= _check("t6 nothing queued", api.commands == [], "commands {}".format(api.commands))

    print("Test 7: automatic_config wires the switch for active devices only, alongside the dispatch sensor")
    # automatic_config() reads back the slot wiring it wrote, so this uses the real args, restored afterwards
    # because my_predbat is shared by every test in the run
    original_args = dict(my_predbat.args)
    del api.set_arg
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False
    api.intelligent_devices[SUSPENDED_ID]["suspended"] = True
    api.automatic_config({})
    failed |= _check("t7 switch wired", my_predbat.args.get("octopus_intelligent_smart_control") == [active_switch], "args {}".format(my_predbat.args))
    failed |= _check("t7 slot wired alongside", len(my_predbat.args.get("octopus_intelligent_slot", [])) == 1, "args {}".format(my_predbat.args))
    api.intelligent_devices[ACTIVE_ID]["suspended"] = True
    api.automatic_config({})
    failed |= _check("t7 none when all suspended", my_predbat.args.get("octopus_intelligent_smart_control") == [], "args {}".format(my_predbat.args))
    failed |= _check("t7 switches still published for suspended", api.published.get(active_switch) is not None, "")
    my_predbat.args.clear()
    my_predbat.args.update(original_args)
    return failed


def test_octopus_smart_control_switch(my_predbat):
    """
    Run the Smart Control switch tests of the built-in Octopus component.
    """
    print("*** Running test: octopus_smart_control_switch")
    failed = asyncio.run(_run(my_predbat))
    print("*** octopus_smart_control_switch test {}".format("FAILED" if failed else "PASSED"))
    return failed
