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
    await api.switch_event(suspended_switch, "turn_on")
    failed |= _check("t3 state", api.published.get(suspended_switch) == "on", "published {}".format(api.published))
    api.async_graphql_query = AsyncMock(return_value={"updateDeviceSmartControl": {"id": SUSPENDED_ID}})
    await api.process_commands("acc-1")
    mutation = api.async_graphql_query.call_args[0][0]
    failed |= _check("t3 mutation", "action: UNSUSPEND" in mutation and SUSPENDED_ID in mutation, "mutation {}".format(mutation))
    failed |= _check("t3 cached", api.intelligent_devices[SUSPENDED_ID]["suspended"] is False, "")

    print("Test 4: toggle flips the state")
    api.commands = []
    await api.switch_event(active_switch, "toggle")
    failed |= _check("t4 toggled on", api.published.get(active_switch) == "on" and api.commands[0]["value"] is True, "commands {}".format(api.commands))
    api.commands = []

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
        api.intelligent_devices[ACTIVE_ID]["suspended"] = False
        await api.switch_event(active_switch, "turn_off")
        api.async_graphql_query = AsyncMock(side_effect=reply) if isinstance(reply, Exception) else AsyncMock(return_value=reply)
        await api.process_commands("acc-1")
        failed |= _check("t5b rolled back {}".format(type(reply).__name__), api.published.get(active_switch) == "on" and api.intelligent_devices[ACTIVE_ID]["suspended"] is False, "published {}".format(api.published))

    print("Test 5c: a poll straight after a successful change does not flip the switch back until Octopus reports it")
    api.commands = []
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

    print("Test 6: events for other entities, other services and unknown devices are ignored")
    api.commands = []
    await api.switch_event("switch.predbat_octopus_acc_1_something_else_1001", "turn_off")
    await api.switch_event(active_switch, "bogus")
    await api.switch_event(api.get_entity_name("switch", "intelligent_smart_charge", index="9999"), "turn_off")
    failed |= _check("t6 nothing queued", api.commands == [], "commands {}".format(api.commands))

    print("Test 7: automatic_config wires the switch for active devices only, alongside the dispatch sensor")
    api.intelligent_devices[ACTIVE_ID]["suspended"] = False
    api.intelligent_devices[SUSPENDED_ID]["suspended"] = True
    api.automatic_config({})
    failed |= _check("t7 switch wired", api.args_set.get("octopus_intelligent_smart_control") == [active_switch], "args {}".format(api.args_set))
    failed |= _check("t7 slot wired alongside", len(api.args_set.get("octopus_intelligent_slot", [])) == 1, "args {}".format(api.args_set))
    api.intelligent_devices[ACTIVE_ID]["suspended"] = True
    api.automatic_config({})
    failed |= _check("t7 none when all suspended", api.args_set.get("octopus_intelligent_smart_control") == [], "args {}".format(api.args_set))
    failed |= _check("t7 switches still published for suspended", api.published.get(active_switch) is not None, "")
    return failed


def test_octopus_smart_control_switch(my_predbat):
    """
    Run the Smart Control switch tests of the built-in Octopus component.
    """
    print("*** Running test: octopus_smart_control_switch")
    failed = asyncio.run(_run(my_predbat))
    print("*** octopus_smart_control_switch test {}".format("FAILED" if failed else "PASSED"))
    return failed
