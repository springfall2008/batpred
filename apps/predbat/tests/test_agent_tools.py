# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the shared agent tool layer extracted from the MCP server.

The golden-list test is the contract guard: the MCP tools/list output must be byte-identical
before and after the extraction, or an MCP client's tool set changed without anyone deciding it.
"""

import asyncio
import json
import os
import time

from agent_tools import TOOL_DEFS, PredbatTools, mcp_tool_list, openai_tool_list, slim_plan, compile_filter_argument, MCPArgumentError, FILTER_PATTERN_MAX
from utils import mask_secret_args
from web_mcp import MCPServerWrapper

GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_tools_golden.json")

WRITE_TOOLS = {"set_config", "set_plan_override"}


def test_tool_defs_integrity(my_predbat):
    """Every TOOL_DEFS entry maps to a handler, and the writes flags name exactly the two writers."""
    failed = False
    print("**** Testing TOOL_DEFS integrity ****")
    tools = PredbatTools(my_predbat, log_func=my_predbat.log)

    for entry in TOOL_DEFS:
        for field in ("name", "description", "parameters", "writes"):
            if field not in entry:
                print("ERROR: TOOL_DEFS entry {} is missing '{}'".format(entry.get("name"), field))
                failed = True
        handler = getattr(tools, "_execute_{}".format(entry["name"]), None)
        if handler is None:
            print("ERROR: no handler _execute_{} for TOOL_DEFS entry".format(entry["name"]))
            failed = True

    declared = {entry["name"] for entry in TOOL_DEFS}
    writers = {entry["name"] for entry in TOOL_DEFS if entry["writes"]}
    if writers != WRITE_TOOLS:
        print("ERROR: writes flags are {}, expected {}".format(sorted(writers), sorted(WRITE_TOOLS)))
        failed = True

    for name in dir(tools):
        if name.startswith("_execute_") and name[len("_execute_") :] not in declared:
            print("ERROR: handler {} has no TOOL_DEFS entry".format(name))
            failed = True

    return failed


def test_mcp_tool_list_matches_golden(my_predbat):
    """mcp_tool_list() reproduces the pre-refactor tools/list output exactly."""
    failed = False
    print("**** Testing mcp_tool_list() against the golden file ****")
    with open(GOLDEN_PATH, "r", encoding="utf-8") as handle:
        golden = json.load(handle)

    produced = {"tools": mcp_tool_list()}
    if json.dumps(produced, sort_keys=True) != json.dumps(golden, sort_keys=True):
        print("ERROR: tools/list changed. Produced:\n{}".format(json.dumps(produced, indent=2, sort_keys=True)))
        failed = True

    mcp = MCPServerWrapper(my_predbat, log_func=my_predbat.log)
    live = asyncio.run(mcp._handle_tools_list({}))
    if json.dumps(live, sort_keys=True) != json.dumps(golden, sort_keys=True):
        print("ERROR: MCPServerWrapper._handle_tools_list no longer matches the golden file")
        failed = True

    return failed


def test_openai_tool_list_shape(my_predbat):
    """openai_tool_list() is well-formed function-calling shape and strips chat_omit_properties.

    This only checks the schema offered to the model - a presentation detail. Removing a property
    from the schema does not, by itself, stop the model naming it anyway: the guarantee that
    'masked' cannot reach the real get_apps call has to be enforced where the tool actually runs,
    which is ChatAgent._dispatch(). That enforcement is exercised (and mutation-checked) by
    test_chat.py's test_dispatch_strips_chat_omit_properties - this test only guards the schema
    not silently growing 'masked' back in.
    """
    failed = False
    print("**** Testing openai_tool_list() shape ****")
    listed = openai_tool_list()

    if len(listed) != len(TOOL_DEFS):
        print("ERROR: openai_tool_list() returned {} entries, expected {}".format(len(listed), len(TOOL_DEFS)))
        failed = True

    for entry in listed:
        if entry.get("type") != "function" or "function" not in entry:
            print("ERROR: malformed OpenAI tool entry: {}".format(entry))
            failed = True
            continue
        function = entry["function"]
        for field in ("name", "description", "parameters"):
            if field not in function:
                print("ERROR: OpenAI tool {} is missing '{}'".format(function.get("name"), field))
                failed = True

    chat_apps = [e["function"] for e in listed if e["function"]["name"] == "get_apps"][0]
    if "masked" in chat_apps["parameters"].get("properties", {}):
        print("ERROR: get_apps still exposes 'masked' in the chat schema projection")
        failed = True

    mcp_apps = [e for e in mcp_tool_list() if e["name"] == "get_apps"][0]
    if "masked" not in mcp_apps["inputSchema"].get("properties", {}):
        print("ERROR: get_apps lost 'masked' from the MCP projection")
        failed = True

    return failed


def test_execute_dispatch(my_predbat):
    """execute() reaches a real handler and reports an unknown tool rather than raising."""
    failed = False
    print("**** Testing PredbatTools.execute() dispatch ****")
    tools = PredbatTools(my_predbat, log_func=my_predbat.log)

    result = asyncio.run(tools.execute("get_config", {}))
    if not result.get("success"):
        print("ERROR: execute('get_config') failed: {}".format(result.get("error")))
        failed = True

    result = asyncio.run(tools.execute("no_such_tool", {}))
    if result.get("success") or "no_such_tool" not in str(result.get("error")):
        print("ERROR: unknown tool did not report cleanly: {}".format(result))
        failed = True

    return failed


def test_mcp_wrapper_still_inherits(my_predbat):
    """MCPServerWrapper keeps the _execute_* methods its existing tests call directly."""
    failed = False
    print("**** Testing MCPServerWrapper inheritance ****")
    mcp = MCPServerWrapper(my_predbat, log_func=my_predbat.log)
    for name in ("_execute_get_apps", "_execute_get_log", "_execute_get_state", "_execute_get_plan"):
        if not callable(getattr(mcp, name, None)):
            print("ERROR: MCPServerWrapper lost {}".format(name))
            failed = True
    return failed


def test_handler_crash_sets_is_error(my_predbat):
    """A handler that raises (rather than returning its own {success: False}) propagates through
    execute() and is caught by _handle_tools_call(), which still reports isError: True at the MCP
    protocol level - the contract PredbatTools.execute() must not swallow (review finding on
    the tool-layer extraction).
    """
    failed = False
    print("**** Testing that a raising handler still sets isError over MCP ****")
    mcp = MCPServerWrapper(my_predbat, log_func=my_predbat.log)

    async def _boom(arguments):
        """Stand-in handler that crashes instead of returning a result dict."""
        raise RuntimeError("simulated handler crash")

    saved_handler = mcp._execute_get_status
    try:
        mcp._execute_get_status = _boom
        result = asyncio.run(mcp._handle_tools_call({"name": "get_status", "arguments": {}}))
        if not result.get("isError"):
            print("ERROR: expected isError to be set when a handler raises, got {}".format(result))
            failed = True
        decoded = json.loads(result["content"][0]["text"])
        if decoded.get("success") or "simulated handler crash" not in str(decoded.get("error")):
            print("ERROR: expected the crash to be reported by name, got {}".format(decoded))
            failed = True
    finally:
        mcp._execute_get_status = saved_handler

    return failed


def _set_ha_state_switch(my_predbat, value):
    """Set ai_ha_state_enable's config value directly, returning the previous value to restore."""
    original = my_predbat.config_index["ai_ha_state_enable"].get("value")
    my_predbat.config_index["ai_ha_state_enable"]["value"] = value
    return original


def _restore_history_wrapper(my_predbat):
    """Undo a get_history_wrapper monkey-patch, restoring the class method rather than leaving an
    instance-level override behind for later tests to inherit (get_history_wrapper has no
    instance attribute of its own outside a test, so reassigning the captured bound method back
    would itself leave a permanent shadow - deleting the instance override is what actually
    restores it, matching test_calculate_yesterday.py's own restore helper)."""
    if hasattr(my_predbat.__class__, "get_history_wrapper"):
        try:
            del my_predbat.get_history_wrapper
        except AttributeError:
            pass


def _restore_get_state(my_predbat):
    """Undo a ha_interface.get_state monkey-patch, restoring the class method the same way
    _restore_history_wrapper does for get_history_wrapper."""
    if hasattr(my_predbat.ha_interface.__class__, "get_state"):
        try:
            del my_predbat.ha_interface.get_state
        except AttributeError:
            pass


def _make_history_stub(calls, records):
    """Return a get_history_wrapper stand-in that records each call's arguments and hands back
    a fixed set of history records, in the [[record, ...]] shape get_history_wrapper returns."""

    def _stub(entity_id, days=30, required=True, tracked=True):
        """Record this call's arguments and return the fixed records."""
        calls.append({"entity_id": entity_id, "days": days, "required": required, "tracked": tracked})
        return [records]

    return _stub


def test_ha_state_tools_gate(my_predbat):
    """search_entities/get_entity_state/get_entity_history refuse cleanly, naming the switch, when
    ai_ha_state_enable is off - and, unlike a test that only checks the error string, this also
    proves each tool never touched its data source at all rather than running and discarding the
    result. The reverse direction (switch on -> the tool actually reaches its data source) is
    checked too, so a gate that accidentally denies everything permanently would also be caught.
    """
    failed = False
    print("**** Testing ai_ha_state_enable gate ****")
    tools = PredbatTools(my_predbat, log_func=my_predbat.log)

    original_value = _set_ha_state_switch(my_predbat, False)
    get_state_calls = []
    get_history_calls = []
    original_get_state = my_predbat.ha_interface.get_state
    history_args = {"entity_id": "sensor.does_not_matter", "start": "2026-07-23T00:00:00+00:00", "end": "2026-07-23T01:00:00+00:00"}

    def spy_get_state(*args, **kwargs):
        """Record that get_state was reached, then behave exactly as normal."""
        get_state_calls.append((args, kwargs))
        return original_get_state(*args, **kwargs)

    my_predbat.ha_interface.get_state = spy_get_state
    my_predbat.get_history_wrapper = _make_history_stub(get_history_calls, [])

    try:
        for name, arguments in (("search_entities", {"pattern": ".*"}), ("get_entity_state", {"entity_id": "sensor.does_not_matter"}), ("get_entity_history", history_args)):
            result = asyncio.run(tools.execute(name, arguments))
            if result.get("success"):
                print("ERROR: {} succeeded while ai_ha_state_enable is off".format(name))
                failed = True
            if "switch.predbat_ai_ha_state_enable" not in str(result.get("error")):
                print("ERROR: {} did not name the switch in its refusal: {}".format(name, result))
                failed = True

        if get_state_calls:
            print("ERROR: the HA interface was queried even though the switch is off: {}".format(get_state_calls))
            failed = True
        if get_history_calls:
            print("ERROR: get_history_wrapper was called even though the switch is off: {}".format(get_history_calls))
            failed = True

        # Reverse direction: with the switch on, each tool actually reaches its data source.
        _set_ha_state_switch(my_predbat, True)
        result = asyncio.run(tools.execute("search_entities", {"pattern": ".*"}))
        if not result.get("success") or not get_state_calls:
            print("ERROR: search_entities did not reach the HA interface once the switch is on: {}".format(result))
            failed = True
        asyncio.run(tools.execute("get_entity_history", history_args))
        if not get_history_calls:
            print("ERROR: get_entity_history did not reach get_history_wrapper once the switch is on")
            failed = True
    finally:
        _restore_get_state(my_predbat)
        _restore_history_wrapper(my_predbat)
        _set_ha_state_switch(my_predbat, original_value)

    return failed


def test_search_entities(my_predbat):
    """search_entities regex-matches every HA entity id, caps results at 'limit', reports
    total_matches separately so a truncated result is visibly truncated, and never returns
    attribute dicts - that split is what get_entity_state is for.
    """
    failed = False
    print("**** Testing search_entities ****")
    tools = PredbatTools(my_predbat, log_func=my_predbat.log)

    original_value = _set_ha_state_switch(my_predbat, True)
    probe_ids = ["binary_sensor.agent_tools_probe_door_1", "binary_sensor.agent_tools_probe_door_2", "binary_sensor.agent_tools_probe_door_3"]

    try:
        for index, entity_id in enumerate(probe_ids):
            my_predbat.ha_interface.dummy_items[entity_id] = {"state": "on" if index % 2 == 0 else "off", "friendly_name": "Probe door {}".format(index), "last_changed": "2026-07-23T10:00:00+00:00"}

        result = asyncio.run(tools.execute("search_entities", {"pattern": "agent_tools_probe_door", "limit": 2}))
        if not result.get("success"):
            print("ERROR: search_entities failed: {}".format(result))
            failed = True
        data = result.get("data") or {}
        if data.get("total_matches") != 3:
            print("ERROR: expected total_matches=3, got {}".format(data.get("total_matches")))
            failed = True
        entities = data.get("entities") or []
        if len(entities) != 2:
            print("ERROR: expected limit=2 to cap the returned entities at 2, got {}".format(len(entities)))
            failed = True
        for entry in entities:
            if set(entry.keys()) != {"entity_id", "state", "last_changed"}:
                print("ERROR: search_entities returned attribute data - get_entity_state exists to keep that separate: {}".format(entry))
                failed = True

        # A pattern that matches nothing is a clean, empty result, not an error
        result = asyncio.run(tools.execute("search_entities", {"pattern": "no_such_entity_will_ever_match_this"}))
        if not result.get("success") or (result.get("data") or {}).get("total_matches") != 0:
            print("ERROR: a non-matching pattern should succeed with zero matches: {}".format(result))
            failed = True

        # A bad regex is a clean argument error, not an exception
        result = asyncio.run(tools.execute("search_entities", {"pattern": "("}))
        if result.get("success") or "pattern" not in str(result.get("error")):
            print("ERROR: a bad regex should be reported as a named argument error: {}".format(result))
            failed = True
    finally:
        for entity_id in probe_ids:
            my_predbat.ha_interface.dummy_items.pop(entity_id, None)
        _set_ha_state_switch(my_predbat, original_value)

    return failed


def test_get_entity_state(my_predbat):
    """get_entity_state returns one entity's state, includes attributes only when asked (kept
    separate because attribute dicts are bulky), and reports an unknown entity as a clean failure
    rather than raising.
    """
    failed = False
    print("**** Testing get_entity_state ****")
    tools = PredbatTools(my_predbat, log_func=my_predbat.log)

    original_value = _set_ha_state_switch(my_predbat, True)
    entity_id = "sensor.agent_tools_probe_temperature"

    try:
        my_predbat.ha_interface.dummy_items[entity_id] = {"state": "21.5", "unit_of_measurement": "C", "friendly_name": "Probe temperature", "last_changed": "2026-07-23T10:00:00+00:00"}

        result = asyncio.run(tools.execute("get_entity_state", {"entity_id": entity_id}))
        if not result.get("success"):
            print("ERROR: get_entity_state failed for a known entity: {}".format(result))
            failed = True
        data = result.get("data") or {}
        if data.get("state") != "21.5" or "attributes" in data:
            print("ERROR: expected state only (no attributes) by default: {}".format(data))
            failed = True

        result = asyncio.run(tools.execute("get_entity_state", {"entity_id": entity_id, "attributes": True}))
        data = result.get("data") or {}
        if data.get("attributes", {}).get("unit_of_measurement") != "C":
            print("ERROR: expected attributes to be included when asked: {}".format(data))
            failed = True

        result = asyncio.run(tools.execute("get_entity_state", {"entity_id": "sensor.this_entity_does_not_exist_anywhere"}))
        if result.get("success") or not result.get("error"):
            print("ERROR: an unknown entity should report success: False with an error message, got {}".format(result))
            failed = True
    finally:
        my_predbat.ha_interface.dummy_items.pop(entity_id, None)
        _set_ha_state_switch(my_predbat, original_value)

    return failed


def test_get_entity_history_numeric(my_predbat):
    """Numeric-mode buckets report min/max/mean/count, and unavailable readings are skipped from
    the statistics while still being counted. The first bucket holds three distinct real readings
    (not just one), so min, max and mean cannot be confused with each other by coincidence.
    """
    failed = False
    print("**** Testing get_entity_history (numeric mode) ****")
    tools = PredbatTools(my_predbat, log_func=my_predbat.log)
    original_value = _set_ha_state_switch(my_predbat, True)

    records = [
        {"last_updated": "2026-07-23T10:05:00+00:00", "state": "10"},
        {"last_updated": "2026-07-23T10:10:00+00:00", "state": "20"},
        {"last_updated": "2026-07-23T10:15:00+00:00", "state": "unavailable"},
        {"last_updated": "2026-07-23T10:35:00+00:00", "state": "30"},
    ]
    calls = []
    my_predbat.get_history_wrapper = _make_history_stub(calls, records)

    try:
        arguments = {"entity_id": "sensor.agent_tools_probe_power", "start": "2026-07-23T10:00:00+00:00", "end": "2026-07-23T11:00:00+00:00", "bucket_minutes": 30}
        result = asyncio.run(tools.execute("get_entity_history", arguments))
        if not result.get("success"):
            print("ERROR: get_entity_history failed: {}".format(result))
            failed = True
        data = result.get("data") or {}
        if data.get("mode") != "numeric":
            print("ERROR: expected numeric mode, got {}".format(data.get("mode")))
            failed = True
        buckets = data.get("buckets") or []
        if len(buckets) != 2:
            print("ERROR: expected 2 buckets, got {}".format(len(buckets)))
            failed = True
        else:
            first, second = buckets
            if (first.get("min"), first.get("max"), first.get("mean"), first.get("count"), first.get("unavailable")) != (10, 20, 15, 2, 1):
                print("ERROR: unexpected first bucket stats: {}".format(first))
                failed = True
            if (second.get("min"), second.get("max"), second.get("mean"), second.get("count"), second.get("unavailable")) != (30, 30, 30, 1, 0):
                print("ERROR: unexpected second bucket stats: {}".format(second))
                failed = True
        if not calls or calls[0]["tracked"] is not False:
            print("ERROR: expected get_history_wrapper to be called with tracked=False (an ad hoc lookup, not one of Predbat's own tracked series), got {}".format(calls))
            failed = True
    finally:
        _restore_history_wrapper(my_predbat)
        _set_ha_state_switch(my_predbat, original_value)

    return failed


def test_get_entity_history_text(my_predbat):
    """Text-mode buckets report first/last/changes rather than a most-common value, so a sensor
    that flaps is visible as having flapped. An 'unknown' reading in the middle of the window is
    excluded from first/last/changes entirely, rather than being treated as a real value.
    """
    failed = False
    print("**** Testing get_entity_history (text mode) ****")
    tools = PredbatTools(my_predbat, log_func=my_predbat.log)
    original_value = _set_ha_state_switch(my_predbat, True)

    records = [
        {"last_updated": "2026-07-23T10:05:00+00:00", "state": "Idle"},
        {"last_updated": "2026-07-23T10:07:00+00:00", "state": "unknown"},
        {"last_updated": "2026-07-23T10:10:00+00:00", "state": "Idle"},
        {"last_updated": "2026-07-23T10:15:00+00:00", "state": "Charging"},
        {"last_updated": "2026-07-23T10:20:00+00:00", "state": "Charging"},
        {"last_updated": "2026-07-23T10:25:00+00:00", "state": "Idle"},
    ]
    calls = []
    my_predbat.get_history_wrapper = _make_history_stub(calls, records)

    try:
        arguments = {"entity_id": "predbat.status", "start": "2026-07-23T10:00:00+00:00", "end": "2026-07-23T10:30:00+00:00", "bucket_minutes": 30}
        result = asyncio.run(tools.execute("get_entity_history", arguments))
        data = result.get("data") or {}
        if data.get("mode") != "text":
            print("ERROR: expected text mode, got {}".format(data.get("mode")))
            failed = True
        buckets = data.get("buckets") or []
        if len(buckets) != 1:
            print("ERROR: expected 1 bucket, got {}".format(len(buckets)))
            failed = True
        else:
            bucket = buckets[0]
            if (bucket.get("first"), bucket.get("last"), bucket.get("changes")) != ("Idle", "Idle", 2):
                print("ERROR: unexpected text bucket, expected first=Idle last=Idle changes=2 (the 'unknown' reading excluded): {}".format(bucket))
                failed = True
            if "unavailable" in bucket or "min" in bucket:
                print("ERROR: a text-mode bucket should not carry numeric-mode keys: {}".format(bucket))
                failed = True
    finally:
        _restore_history_wrapper(my_predbat)
        _set_ha_state_switch(my_predbat, original_value)

    return failed


def test_get_entity_history_attribute_numeric(my_predbat):
    """get_entity_history(attribute=...) buckets the named attribute, not the state. The fixture's
    state ('Idle', non-numeric) and attribute ('power', numeric) deliberately differ, so a
    regression that silently read the state instead of the attribute would fail this test rather
    than coincidentally passing it.

    Mutation-checked: making _bucket_entity_history read record["state"] regardless of 'attribute'
    turns 'power' ('100'/'300', numeric) into 'Idle' (non-numeric) for every record, which drops
    every value out as unavailable and fails the min/max/mean/count assertions below - confirmed
    by hand while writing this test, then reverted.
    """
    failed = False
    print("**** Testing get_entity_history(attribute=...) numeric case ****")
    tools = PredbatTools(my_predbat, log_func=my_predbat.log)
    original_value = _set_ha_state_switch(my_predbat, True)

    records = [
        {"last_updated": "2026-07-23T10:05:00+00:00", "state": "Idle", "attributes": {"power": "100"}},
        {"last_updated": "2026-07-23T10:15:00+00:00", "state": "Idle", "attributes": {"power": "300"}},
    ]
    calls = []
    my_predbat.get_history_wrapper = _make_history_stub(calls, records)

    try:
        arguments = {"entity_id": "sensor.agent_tools_probe_charger", "start": "2026-07-23T10:00:00+00:00", "end": "2026-07-23T10:30:00+00:00", "bucket_minutes": 30, "attribute": "power"}
        result = asyncio.run(tools.execute("get_entity_history", arguments))
        data = result.get("data") or {}
        if data.get("attribute") != "power":
            print("ERROR: expected the response to name the attribute it bucketed: {}".format(data))
            failed = True
        if data.get("mode") != "numeric":
            print("ERROR: expected numeric mode from the attribute values, got {}".format(data.get("mode")))
            failed = True
        buckets = data.get("buckets") or []
        bucket = buckets[0] if buckets else {}
        if (bucket.get("min"), bucket.get("max"), bucket.get("mean"), bucket.get("count"), bucket.get("unavailable")) != (100, 300, 200, 2, 0):
            print("ERROR: expected stats from the 'power' attribute (100/300), not the 'Idle' state: {}".format(bucket))
            failed = True
    finally:
        _restore_history_wrapper(my_predbat)
        _set_ha_state_switch(my_predbat, original_value)

    return failed


def test_get_entity_history_attribute_text(my_predbat):
    """get_entity_history(attribute=...) also takes the first/last/changes shape when the named
    attribute is text, exactly as the state case does.
    """
    failed = False
    print("**** Testing get_entity_history(attribute=...) text case ****")
    tools = PredbatTools(my_predbat, log_func=my_predbat.log)
    original_value = _set_ha_state_switch(my_predbat, True)

    records = [
        {"last_updated": "2026-07-23T10:05:00+00:00", "state": "50", "attributes": {"mode": "Idle"}},
        {"last_updated": "2026-07-23T10:15:00+00:00", "state": "60", "attributes": {"mode": "Charging"}},
        {"last_updated": "2026-07-23T10:20:00+00:00", "state": "70", "attributes": {"mode": "Charging"}},
    ]
    calls = []
    my_predbat.get_history_wrapper = _make_history_stub(calls, records)

    try:
        arguments = {"entity_id": "sensor.agent_tools_probe_charger", "start": "2026-07-23T10:00:00+00:00", "end": "2026-07-23T10:30:00+00:00", "bucket_minutes": 30, "attribute": "mode"}
        result = asyncio.run(tools.execute("get_entity_history", arguments))
        data = result.get("data") or {}
        if data.get("mode") != "text":
            print("ERROR: expected text mode from the 'mode' attribute's values, got {}".format(data.get("mode")))
            failed = True
        buckets = data.get("buckets") or []
        bucket = buckets[0] if buckets else {}
        if (bucket.get("first"), bucket.get("last"), bucket.get("changes")) != ("Idle", "Charging", 1):
            print("ERROR: unexpected text bucket for the 'mode' attribute: {}".format(bucket))
            failed = True
    finally:
        _restore_history_wrapper(my_predbat)
        _set_ha_state_switch(my_predbat, original_value)

    return failed


def test_get_entity_history_bucket_cap(my_predbat):
    """The bucket count is capped at 500 by pulling 'end' in, not by widening the buckets - a
    caller that asked for 1-minute buckets over a whole day gets 500 of them and a truncation
    flag, not 1440 fine buckets merged into something coarser than it asked for.
    """
    failed = False
    print("**** Testing get_entity_history bucket cap ****")
    tools = PredbatTools(my_predbat, log_func=my_predbat.log)
    original_value = _set_ha_state_switch(my_predbat, True)
    my_predbat.get_history_wrapper = _make_history_stub([], [])

    try:
        arguments = {"entity_id": "sensor.agent_tools_probe_cap", "start": "2026-07-23T00:00:00+00:00", "end": "2026-07-24T00:00:00+00:00", "bucket_minutes": 1}
        result = asyncio.run(tools.execute("get_entity_history", arguments))
        data = result.get("data") or {}
        if data.get("bucket_count") != 500 or len(data.get("buckets") or []) != 500:
            print("ERROR: expected the bucket count capped at 500, got {}".format(data.get("bucket_count")))
            failed = True
        if not data.get("range_truncated"):
            print("ERROR: expected range_truncated to be reported when the cap bites")
            failed = True
        if data.get("end") != "2026-07-23T08:20:00+00:00":
            print("ERROR: expected 'end' pulled in to start + 500 minutes, got {}".format(data.get("end")))
            failed = True
    finally:
        _restore_history_wrapper(my_predbat)
        _set_ha_state_switch(my_predbat, original_value)

    return failed


def test_get_entity_history_lookback_clamp(my_predbat):
    """A window starting more than 30 days before 'end' is clamped to the 30-day maximum
    lookback, and the response says so - this bounds the fetch, distinct from the bucket cap
    above, which bounds the response. bucket_minutes is set generously (1 day) so the 500-bucket
    cap cannot also fire and confound which guard actually clamped the result.
    """
    failed = False
    print("**** Testing get_entity_history lookback clamp ****")
    tools = PredbatTools(my_predbat, log_func=my_predbat.log)
    original_value = _set_ha_state_switch(my_predbat, True)
    calls = []
    my_predbat.get_history_wrapper = _make_history_stub(calls, [])

    try:
        arguments = {"entity_id": "sensor.agent_tools_probe_lookback", "start": "2026-06-01T00:00:00+00:00", "end": "2026-08-01T00:00:00+00:00", "bucket_minutes": 1440}
        result = asyncio.run(tools.execute("get_entity_history", arguments))
        data = result.get("data") or {}
        if not data.get("lookback_clamped"):
            print("ERROR: expected lookback_clamped when 'start' is over 30 days before 'end'")
            failed = True
        if data.get("range_truncated"):
            print("ERROR: expected the bucket cap not to fire in this test - bucket_minutes was chosen to avoid it: {}".format(data))
            failed = True
        if data.get("start") != "2026-07-02T00:00:00+00:00":
            print("ERROR: expected 'start' clamped to 30 days before 'end', got {}".format(data.get("start")))
            failed = True
        if not calls or calls[0]["days"] > 32:
            print("ERROR: expected the fetch itself to be bounded by the clamp, not the original 61-day request: {}".format(calls))
            failed = True
    finally:
        _restore_history_wrapper(my_predbat)
        _set_ha_state_switch(my_predbat, original_value)

    return failed


def test_nested_credentials_are_redacted(my_predbat):
    """A credential nested inside a non-credential key is redacted, not returned in the clear.

    mask_secret_args used to walk only top-level key names. The shipped apps.yaml template
    documents forecast_solar as a list of dicts each carrying its own api_key, and
    'forecast_solar' matches none of SECRET_KEY_SUBSTRINGS - so that key was handed to whichever
    third-party model asked, through get_apps, get_apps_config and get_state alike.

    Mutation check: reverting mask_secret_args to its top-level-only loop fails this on every
    assertion below.
    """
    failed = False
    print("**** Testing nested credentials are redacted ****")

    original_args = my_predbat.args
    try:
        my_predbat.args = {
            "forecast_solar": [{"postcode": "SW1A 1AA", "kwp": 4.0, "api_key": "REAL-FORECAST-SOLAR-KEY"}],
            "inverters": [{"name": "one", "nested": {"password": "REAL-PASSWORD"}}],
            "battery_size": 9.5,
            "ha_key": "REAL-HA-TOKEN",
        }

        masked = mask_secret_args(my_predbat.args)
        if masked["forecast_solar"][0]["api_key"] != "xxx":
            print("ERROR: nested api_key was not redacted: {}".format(masked["forecast_solar"]))
            failed = True
        if masked["inverters"][0]["nested"]["password"] != "xxx":
            print("ERROR: a credential two levels down was not redacted: {}".format(masked["inverters"]))
            failed = True
        if masked["ha_key"] != "xxx":
            print("ERROR: a top-level credential stopped being redacted: {}".format(masked))
            failed = True
        if masked["battery_size"] != 9.5 or masked["forecast_solar"][0]["postcode"] != "SW1A 1AA":
            print("ERROR: redaction damaged non-credential values: {}".format(masked))
            failed = True
        if my_predbat.args["forecast_solar"][0]["api_key"] != "REAL-FORECAST-SOLAR-KEY":
            print("ERROR: mask_secret_args mutated the live args rather than a copy")
            failed = True

        # And through the tool the model actually calls.
        tools = PredbatTools(my_predbat)
        result = asyncio.run(tools.execute("get_apps_config", {"key": "forecast_solar"}))
        if "REAL-FORECAST-SOLAR-KEY" in json.dumps(result):
            print("ERROR: get_apps_config returned a nested credential: {}".format(result))
            failed = True
        result = asyncio.run(tools.execute("get_apps", {}))
        if "REAL-FORECAST-SOLAR-KEY" in json.dumps(result):
            print("ERROR: get_apps returned a nested credential: {}".format(result))
            failed = True
    finally:
        my_predbat.args = original_args
    return failed


def test_pathological_regex_arguments_are_rejected(my_predbat):
    """A nested-quantifier regex is refused before it can be compiled and run.

    search_entities runs a model-supplied regex over every entity id on the component's only
    event loop, with no await in the loop - the exact hazard search_source is offloaded to a
    worker thread to avoid. Python's re backtracks with no timeout, so '(.+)+@' against a typical
    40-character entity id does not finish in any practical time, and the loop it is running on
    serves every other chat and MCP request. Guarding at compile_filter_argument covers
    search_entities and the 'filter' arguments on get_state, get_apps and get_config at once.

    Mutation check: removing the reject_pathological_pattern() call from compile_filter_argument
    makes the refusal assertions below fail.
    """
    failed = False
    print("**** Testing pathological regex arguments are rejected ****")

    for pattern in ("(.+)+@", "(a+)+$", "(x*)*y", "([a-z]+)+#", "(ab|a)+{2,}"):
        try:
            compile_filter_argument(pattern, "pattern")
            print("ERROR: pathological pattern {!r} was accepted".format(pattern))
            failed = True
        except MCPArgumentError:
            pass

    if len("a" * (FILTER_PATTERN_MAX + 1)) <= FILTER_PATTERN_MAX:
        print("ERROR: test constructed a pattern that is not actually over the cap")
        failed = True
    try:
        compile_filter_argument("a" * (FILTER_PATTERN_MAX + 1), "pattern")
        print("ERROR: an over-long pattern was accepted")
        failed = True
    except MCPArgumentError:
        pass

    # Legitimate searches must still work - a guard that blocks ordinary use is its own bug.
    for pattern in ("sensor.predbat_.*", "(sensor|switch)\\..*battery", ".*(soc|charge).*", "(\\d+)", "^binary_sensor\\."):
        try:
            if compile_filter_argument(pattern, "pattern") is None:
                print("ERROR: legitimate pattern {!r} compiled to None".format(pattern))
                failed = True
        except MCPArgumentError as error:
            print("ERROR: legitimate pattern {!r} was rejected: {}".format(pattern, error))
            failed = True

    # And the refusal reaches the caller as a tool result, not a traceback.
    tools = PredbatTools(my_predbat)
    result = asyncio.run(tools.execute("search_entities", {"pattern": "(.+)+@"}))
    if result.get("success"):
        print("ERROR: search_entities accepted a pathological pattern: {}".format(result))
        failed = True
    return failed


def test_get_entity_history_does_not_block_the_event_loop(my_predbat):
    """The history fetch is offloaded, so the component's event loop keeps running during it.

    get_history_wrapper reaches HAInterface.get_history, which chunks the window at
    HISTORY_CHUNK_DAYS and issues one synchronous requests.get per chunk with a 300s timeout
    each - up to 11 sequential blocking fetches for a 30-day window, none of them cached because
    tracked=False. Run on the event loop, that freezes the chat component and every request
    awaiting run_on_agent_loop with it: /chat/history, /chat/models, the conversation list.

    A heartbeat task on the same loop is the assertion. If the fetch blocks, it cannot tick.

    Mutation check: replacing the run_in_executor call with the plain synchronous call makes
    ticks 0 and this test fails.
    """
    failed = False
    print("**** Testing get_entity_history does not block the event loop ****")

    my_predbat.config_index["ai_ha_state_enable"]["value"] = True
    records = [{"last_updated": "2026-08-27T10:00:00+00:00", "state": "1.0"}]

    def _blocking_stub(entity_id, days=30, required=True, tracked=True):
        """Stand in for the real chunked, synchronous HTTP fetch."""
        time.sleep(0.3)
        return [records]

    original = my_predbat.get_history_wrapper
    my_predbat.get_history_wrapper = _blocking_stub
    try:
        tools = PredbatTools(my_predbat)

        async def drive():
            """Run the tool while a heartbeat ticks on the same loop."""
            ticks = []

            async def heartbeat():
                """Tick every 20ms for as long as the loop is free to run us."""
                try:
                    while True:
                        await asyncio.sleep(0.02)
                        ticks.append(1)
                except asyncio.CancelledError:
                    return

            beat = asyncio.ensure_future(heartbeat())
            await tools.execute("get_entity_history", {"entity_id": "sensor.test", "start": "2026-08-27T10:00:00+00:00", "end": "2026-08-27T12:00:00+00:00"})
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            return len(ticks)

        ticks = asyncio.run(drive())
        # A 0.3s fetch leaves room for ~15 ticks; anything above a couple proves the loop ran.
        if ticks < 3:
            print("ERROR: the event loop was blocked during the history fetch - only {} heartbeat ticks".format(ticks))
            failed = True
    finally:
        my_predbat.get_history_wrapper = original
    return failed


def test_get_apps_config_paths(my_predbat):
    """get_apps_config takes the same paths set_apps_config writes, and masks credentials in them.

    The two halves have to agree: a model that writes 'forecast_solar[0].azimuth' will read the
    same string back to check its work, and a get that only understood top-level names would
    answer "not found" for a key it had just successfully changed.

    Mutation checks: reverting the lookup to 'key not in self.base.args' fails the nested read;
    dropping the per-segment mask check returns the real api_key.
    """
    failed = False
    print("**** Testing get_apps_config paths ****")

    original_args = my_predbat.args
    try:
        my_predbat.args = {
            "num_inverters": 1,
            "forecast_solar": [{"postcode": "SW1A 1AA", "azimuth": 180, "api_key": "REAL-KEY"}],
            "ha_key": "REAL-HA-TOKEN",
        }
        tools = PredbatTools(my_predbat)

        def call(key):
            """Run get_apps_config for one key and return its result dict."""
            return asyncio.run(tools.execute("get_apps_config", {"key": key}))

        # A nested path resolves to the single value.
        result = call("forecast_solar[0].azimuth")
        if not result.get("success") or result["data"]["value"] != 180:
            print("ERROR: nested path did not resolve: {}".format(result))
            failed = True

        # A nested path whose leaf is a credential is masked, not returned.
        result = call("forecast_solar[0].api_key")
        if "REAL-KEY" in json.dumps(result):
            print("ERROR: a nested credential path returned the real key: {}".format(result))
            failed = True
        if not result.get("masked"):
            print("ERROR: a nested credential path was not reported as masked: {}".format(result))
            failed = True

        # Reading the container still masks the credential inside it.
        result = call("forecast_solar")
        if "REAL-KEY" in json.dumps(result):
            print("ERROR: reading the container leaked the nested credential: {}".format(result))
            failed = True

        # Top-level behaviour is unchanged.
        if call("num_inverters")["data"]["value"] != 1:
            print("ERROR: a plain top-level read broke")
            failed = True
        if "REAL-HA-TOKEN" in json.dumps(call("ha_key")):
            print("ERROR: a top-level credential stopped being masked")
            failed = True

        # A credential in a non-leaf segment masks the read. Like the write side, this is what
        # the per-segment check buys over testing the joined path: is_secret_key matches
        # substrings, so "forecast_solar[0].api_key" is caught either way - but
        # "password.token_expires" ends with the exempt suffix "_expires", so the joined string
        # reads as safe while the segment "password" does not. Masking a child of a credential
        # dict is deliberate over-caution: anything hanging off a key called password is assumed
        # sensitive until someone says otherwise.
        my_predbat.args["password"] = {"token_expires": 100}
        tools = PredbatTools(my_predbat)
        result = asyncio.run(tools.execute("get_apps_config", {"key": "password.token_expires"}))
        if not result.get("masked"):
            print("ERROR: a path descending through a credential key was not masked: {}".format(result))
            failed = True

        # A path that does not resolve names what does exist, rather than a bare "not found".
        result = call("forecast_solar[0].no_such_field")
        if result.get("success"):
            print("ERROR: a nonexistent nested path was accepted: {}".format(result))
            failed = True
        error_text = str(result.get("error", ""))
        if "azimuth" not in error_text or "postcode" not in error_text:
            print("ERROR: the not-found error did not list the available keys: {}".format(error_text))
            failed = True

        # An out-of-range index says so, and says the container is a list.
        result = call("forecast_solar[7].azimuth")
        if result.get("success") or "list" not in str(result.get("error", "")):
            print("ERROR: an out-of-range index was not explained as a list: {}".format(result))
            failed = True

        # Reading a container suggests the concrete child path, built from its own keys.
        result = call("forecast_solar")
        if "forecast_solar[0].postcode" not in str(result.get("description", "")):
            print("ERROR: reading a container did not suggest a child path: {}".format(result.get("description")))
            failed = True
    finally:
        my_predbat.args = original_args
    return failed


def test_get_plan_is_slimmed(my_predbat):
    """get_plan strips presentation fields, drops empties, and gives times a weekday.

    The plan structure is built for the web table, so about half of it by volume is styling that
    means nothing to a model: twelve colour fields, HTML cell fragments, and the rowspan/skip
    bookkeeping that merges cells - all repeated on every one of ~96 rows. Measured on a real
    captured plan this halves the response.

    Mutation checks: removing the "color" test, the PLAN_DROP_KEYS test, the empty test, or the
    format_plan_time call each fails an assertion below.
    """
    failed = False
    print("**** Testing get_plan slimming ****")

    row = {
        "time": "2026-08-28T09:00:00+0100",
        "slot_minute": 540,
        "state": "FrzExp",
        "state_target": "",
        "show_limit": "4",
        "reasons": [{"code": "freeze_export", "params": {}}],
        "clipped": 0,
        "car_charging": 0.0,
        "car_rate": None,
        "soc_percent": 85,
        "state_color": "#AAAAAA",
        "rate_color_import": "#F18261",
        "cost_color": "#3AEE85",
        "state_html": "FrzExp&rarr;",
        "state_text": "FrzExp&rarr;",
        "soc_sym": "&rarr;",
        "rowspan_state": 16,
        "skip_state_cell": False,
        "split": False,
    }
    plan = {"rows": [row], "soc": 8.09, "soc_max": 9.52, "mode": "Control charge & discharge", "iboost_enable": None}

    slimmed = slim_plan(plan)
    out = slimmed["rows"][0]

    # Weekday plus clock, not a raw ISO stamp.
    if out.get("time") != "Fri 09:00":
        print("ERROR: the row time was not rendered as weekday + clock: {}".format(out.get("time")))
        failed = True

    # Every colour field goes - including the ones not suffixed "_color".
    for gone in ("state_color", "rate_color_import", "cost_color"):
        if gone in out:
            print("ERROR: colour field {} survived: {}".format(gone, out))
            failed = True

    # HTML and table-layout bookkeeping goes.
    for gone in ("state_html", "state_text", "soc_sym", "rowspan_state", "skip_state_cell", "split"):
        if gone in out:
            print("ERROR: presentation field {} survived: {}".format(gone, out))
            failed = True

    # Empties go, at both levels.
    if "state_target" in out or "car_rate" in out:
        print("ERROR: an empty or null field survived: {}".format(out))
        failed = True
    if "iboost_enable" in slimmed:
        print("ERROR: a null survived at the top level: {}".format(slimmed))
        failed = True

    # Numeric zero is data, not an empty - dropping it would hide "none" behind "not reported".
    for kept, value in (("clipped", 0), ("car_charging", 0.0)):
        if out.get(kept) != value:
            print("ERROR: numeric zero {} was dropped: {}".format(kept, out))
            failed = True

    # The semantic content survives untouched.
    for kept in ("state", "reasons", "show_limit", "slot_minute", "soc_percent"):
        if kept not in out:
            print("ERROR: meaningful field {} was dropped: {}".format(kept, out))
            failed = True
    if slimmed.get("soc") != 8.09 or slimmed.get("mode") != "Control charge & discharge":
        print("ERROR: top-level plan fields were damaged: {}".format(slimmed))
        failed = True

    # The plan handed in is Predbat's live published state - slimming it in place would empty the
    # web plan table for every viewer.
    if "state_color" not in row or row["time"] != "2026-08-28T09:00:00+0100":
        print("ERROR: slim_plan mutated the caller's plan: {}".format(row))
        failed = True

    # An unparseable time is passed through rather than guessed at.
    passthrough = slim_plan({"rows": [{"time": "not a timestamp", "state": "Demand"}]})
    if passthrough["rows"][0]["time"] != "not a timestamp":
        print("ERROR: an unparseable time was not passed through: {}".format(passthrough))
        failed = True

    return failed


def run_agent_tools_tests(my_predbat):
    """Run every shared tool layer test, returning True if any of them failed."""
    failed = False
    failed |= test_tool_defs_integrity(my_predbat)
    failed |= test_mcp_tool_list_matches_golden(my_predbat)
    failed |= test_openai_tool_list_shape(my_predbat)
    failed |= test_execute_dispatch(my_predbat)
    failed |= test_mcp_wrapper_still_inherits(my_predbat)
    failed |= test_handler_crash_sets_is_error(my_predbat)
    failed |= test_ha_state_tools_gate(my_predbat)
    failed |= test_search_entities(my_predbat)
    failed |= test_get_entity_state(my_predbat)
    failed |= test_get_entity_history_numeric(my_predbat)
    failed |= test_get_entity_history_text(my_predbat)
    failed |= test_get_entity_history_attribute_numeric(my_predbat)
    failed |= test_get_entity_history_attribute_text(my_predbat)
    failed |= test_get_entity_history_bucket_cap(my_predbat)
    failed |= test_get_entity_history_lookback_clamp(my_predbat)
    failed |= test_nested_credentials_are_redacted(my_predbat)
    failed |= test_get_apps_config_paths(my_predbat)
    failed |= test_get_plan_is_slimmed(my_predbat)
    failed |= test_pathological_regex_arguments_are_rejected(my_predbat)
    failed |= test_get_entity_history_does_not_block_the_event_loop(my_predbat)
    return failed
