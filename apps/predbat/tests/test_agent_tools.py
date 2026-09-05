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
import copy
import json
import os
import re
import time

from datetime import datetime

from agent_tools import TOOL_DEFS, PredbatTools, format_plan_rows_table, mcp_tool_list, openai_tool_list, slim_plan, slim_plan_rows, compile_filter_argument, MCPArgumentError, FILTER_PATTERN_MAX, parse_log_time_bound
from components import Components, secret_config_names
from utils import is_secret_key, mask_secret_args
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


def test_args_from_apps_yaml_snapshot_is_redacted(my_predbat):
    """The second copy of apps.yaml kept for override detection never serves a credential.

    PredBat.initialize() snapshots self.args into self.args_from_apps_yaml so set_arg_auto() can
    tell "the user configured this" apart from "Predbat defaulted it" (PR #4500). Both dump paths
    that walk self.__dict__ - create_debug_yaml() and the get_state tool - special-cased only the
    field literally named "args" for masking, so this second copy went out in the clear: the
    predbat_debug.yaml users attach to bug reports, every debug_history snapshot, and any model
    calling get_state all received real api keys, tokens and passwords.

    Mutation check: dropping "args_from_apps_yaml" from either masking tuple, or reverting the
    snapshot in initialize() to copy.deepcopy(self.args), fails an assertion below.
    """
    failed = False
    print("**** Testing the args_from_apps_yaml snapshot is redacted ****")

    original_args = my_predbat.args
    original_snapshot = getattr(my_predbat, "args_from_apps_yaml", None)
    try:
        secrets = {
            "ha_key": "REAL-HA-TOKEN",
            "solcast_api_key": "REAL-SOLCAST-KEY",
            "givenergy_password": "REAL-PASSWORD",
            "mcp_secret": "REAL-MCP-SECRET",
            "forecast_solar": [{"postcode": "SW1A 1AA", "api_key": "REAL-NESTED-KEY"}],
            "battery_size": 9.5,
        }
        my_predbat.args = copy.deepcopy(secrets)
        # Planted unmasked on purpose: the guard under test is that neither dump path emits it
        # in the clear, independently of the masking initialize() now applies at the source.
        my_predbat.args_from_apps_yaml = copy.deepcopy(secrets)

        real_values = ["REAL-HA-TOKEN", "REAL-SOLCAST-KEY", "REAL-PASSWORD", "REAL-MCP-SECRET", "REAL-NESTED-KEY"]

        # The debug yaml - what a user attaches to a bug report.
        debug_yaml = my_predbat.create_debug_yaml(write_file=False)
        for secret in real_values:
            if secret in debug_yaml:
                print("ERROR: create_debug_yaml leaked {} from args_from_apps_yaml".format(secret))
                failed = True
        if "battery_size" not in debug_yaml:
            print("ERROR: redaction damaged the debug yaml - non-credential args are missing")
            failed = True

        # The get_state tool - what a model asking for Predbat's state receives.
        tools = PredbatTools(my_predbat)
        result = json.dumps(asyncio.run(tools.execute("get_state", {"keys": ["args_from_apps_yaml"]})))
        for secret in real_values:
            if secret in result:
                print("ERROR: get_state leaked {} from args_from_apps_yaml".format(secret))
                failed = True
        if "xxx" not in result:
            print("ERROR: get_state returned no redaction marker, so the field was not masked: {}".format(result[:400]))
            failed = True

        # Masking must not damage the lookup set_arg_auto actually depends on.
        masked_snapshot = mask_secret_args(secrets)
        if masked_snapshot.get("battery_size") != 9.5:
            print("ERROR: masking the snapshot damaged a non-credential value set_arg_auto compares against")
            failed = True
    finally:
        my_predbat.args = original_args
        if original_snapshot is None:
            if hasattr(my_predbat, "args_from_apps_yaml"):
                del my_predbat.args_from_apps_yaml
        else:
            my_predbat.args_from_apps_yaml = original_snapshot
    return failed


def test_registry_secret_flags_drive_redaction(my_predbat):
    """Every arg the component registry flags "secret": True is redacted everywhere.

    SECRET_KEY_SUBSTRINGS can only catch what a key *name* reveals (_key/password/secret/token),
    so credentials named after what they identify sailed through in the clear: account numbers
    (octopus_api_account, kraken_account_id), meter point numbers (kraken_mpan), site/system/plant
    ids, login identifiers (deye_username, kraken_email, ohme_login, and myenergi_hub_serial,
    which is the digest auth username) and the id half of an id/secret pair (deye_app_id,
    solax_client_id). components.py now flags these explicitly and is_secret_key() honours it.

    Mutation check: dropping the registry_secret_key_names() consultation from is_secret_key(),
    or a "secret": True flag from any of the named args, fails an assertion below.
    """
    failed = False
    print("**** Testing registry secret flags drive redaction ****")

    declared = secret_config_names()
    if not declared:
        print("ERROR: the registry declared no secret args at all - the flag is not wired up")
        failed = True

    # The flag is the contract: whatever it marks must redact, or the declaration is a lie.
    for name in sorted(declared):
        if not is_secret_key(name):
            print("ERROR: {} is flagged secret in components.py but is_secret_key() returns False".format(name))
            failed = True

    # The names that motivated the flag - none of these match any substring.
    newly_covered = [
        "octopus_api_account",
        "kraken_account_id",
        "kraken_export_account_id",
        "kraken_mpan",
        "kraken_export_mpan",
        "kraken_email",
        "myenergi_hub_serial",
        "ohme_login",
        "deye_username",
        "deye_app_id",
        "deye_company_id",
        "sunsynk_username",
        "alphaess_app_id",
        "enphase_username",
        "enphase_site_id",
        "axle_site_id",
        "axle_partner_username",
        "sigenergy_system_id",
        "teslemetry_site_id",
        "solax_client_id",
        "solax_plant_id",
    ]
    for name in newly_covered:
        if name not in declared:
            print("ERROR: {} is no longer flagged secret in components.py".format(name))
            failed = True
        if not is_secret_key(name):
            print("ERROR: {} is a credential but is not redacted".format(name))
            failed = True

    # The registry ADDS to the substring heuristic, it does not replace it. A key belonging to no
    # component at all - a plugin's, a new integration's, one Predbat has never heard of - must
    # still redact on its name alone, or the registry would have narrowed the net rather than
    # widening it. Mutation check: making is_secret_key() consult only the registry fails here.
    for name in ("supabase_key", "some_new_integration_api_key", "whatever_password", "custom_secret", "unknown_token"):
        if name in declared:
            print("ERROR: test assumption broken - {} is a registry arg, so it proves nothing about unknown keys".format(name))
            failed = True
        if not is_secret_key(name):
            print("ERROR: {} matches a credential substring but is no longer redacted".format(name))
            failed = True

    # Over-redaction is its own bug: a serial addresses hardware rather than authenticating it,
    # and it is what makes an integration bug report diagnosable. Same call as the token-expiry
    # exemption - these must stay readable.
    must_stay_visible = [
        "solis_inverter_sn",
        "deye_inverter_sn",
        "alphaess_inverter_sn",
        "sunsynk_inverter_sn",
        "fox_inverter_sn",
        "ge_cloud_serial",
        "solax_plant_sn",
        "kraken_base_url",
        "kraken_provider",
        "deye_auth_method",
        "solis_token_expires_at",
        "carbon_postcode",
        "battery_size",
    ]
    for name in must_stay_visible:
        if is_secret_key(name):
            print("ERROR: {} is not a credential but is being redacted, which hides it from bug reports".format(name))
            failed = True

    # End to end, through what a user downloads and what a model is served.
    original_args = my_predbat.args
    try:
        my_predbat.args = {
            "octopus_api_account": "A-REAL-ACCOUNT",
            "kraken_mpan": "9999999999999",
            "myenergi_hub_serial": "REAL-HUB-SERIAL",
            "solis_inverter_sn": "SN-STAYS-VISIBLE",
            "battery_size": 9.5,
        }
        masked = mask_secret_args(my_predbat.args)
        for key in ("octopus_api_account", "kraken_mpan", "myenergi_hub_serial"):
            if masked[key] != "xxx":
                print("ERROR: {} survived mask_secret_args: {}".format(key, masked[key]))
                failed = True
        if masked["solis_inverter_sn"] != "SN-STAYS-VISIBLE" or masked["battery_size"] != 9.5:
            print("ERROR: redaction damaged values that must stay readable: {}".format(masked))
            failed = True

        tools = PredbatTools(my_predbat)
        result = json.dumps(asyncio.run(tools.execute("get_apps", {})))
        for secret in ("A-REAL-ACCOUNT", "9999999999999", "REAL-HUB-SERIAL"):
            if secret in result:
                print("ERROR: get_apps served {} in the clear".format(secret))
                failed = True
    finally:
        my_predbat.args = original_args

    # The '!secret' advice keeps the narrower scope: it is about values that grant access, and an
    # inline account number is the documented normal setup, so warning about it would be noise.
    # Redaction and this advice are deliberately different questions - see is_secret_key(registry=).
    for name in ("octopus_api_account", "kraken_mpan", "myenergi_hub_serial", "enphase_site_id"):
        if is_secret_key(name, registry=False):
            print("ERROR: {} would now raise a plain-text !secret warning for every user who has it inline".format(name))
            failed = True
    for name in ("ha_key", "solcast_api_key", "kraken_password", "solis_access_token"):
        if not is_secret_key(name, registry=False):
            print("ERROR: {} must still raise the plain-text !secret warning".format(name))
            failed = True

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
        "import_rate_adjusted": 31.84,
        "export_rate_adjusted": 11.4,
        "state": "FrzExp",
        "state_target": "",
        "show_limit": "4",
        "reasons": [{"code": "freeze_export", "params": {}}],
        "clipped": 0,
        "extra_load": 0.15,
        "car_charging": 0.0,
        "car_rate": None,
        "import_rate": 30.26,
        "export_rate": 12.0,
        "pv_forecast": 0.28,
        "pv_forecast10": 0.19,
        "load_forecast": 0.2,
        "load_forecast10": 0.22,
        "soc_percent": 85,
        "soc_change": -1.41,
        "cost_change": -0.01,
        "total_cost": 1.4,
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
    plan = {"rows": [row], "soc": 8.09, "soc_max": 9.52, "mode": "Control charge & discharge", "iboost_enable": None, "currency_symbols": ["\u00a3", "p"], "reason_templates": {"freeze_export": "Freezing export at {target_percent}%"}}

    slimmed = slim_plan(plan)
    # Field-level rules are asserted against the slimmed dicts, not the rendered table: in a table
    # a dropped field and an empty cell look identical, so every "was it dropped?" check below
    # would pass on a field that was merely blank.
    out = slim_plan_rows(plan["rows"])[0]

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

    # The reason templates are the lookup table the dropped reason codes index into. With nothing
    # left to look up they are 2,060 characters describing fields that are no longer sent.
    if "reason_templates" in slimmed:
        print("ERROR: the reason template table outlived the reasons it explains: {}".format(sorted(slimmed)))
        failed = True

    # Numeric zero is data, not an empty - dropping it would hide "none" behind "not reported".
    for kept, value in (("clipped", 0), ("car_charging", 0.0)):
        if out.get(kept) != value:
            print("ERROR: numeric zero {} was dropped: {}".format(kept, out))
            failed = True

    # Content dropped for size, which is a different decision from dropping presentation and is
    # worth asserting separately: two derived rate fields, a timestamp that duplicates `time` once
    # it renders as "Fri 09:00", and the optimiser's reason codes. Measured on a real 73-row plan
    # these were 5,470 characters of reasons and 4,656 of adjusted rates.
    for gone in ("slot_minute", "import_rate_adjusted", "export_rate_adjusted", "reasons"):
        if gone in out:
            print("ERROR: {} was not dropped: {}".format(gone, out))
            failed = True
    # Running totals of columns the table already carries per slot, plus the raw form of the
    # limit the plan page displays. The plan's own top-level "totals" answers the whole-day
    # question these were there for.
    for gone in ("pv_forecast_total", "load_forecast_total", "extra_load_total", "state_target"):
        if gone in out:
            print("ERROR: {} was not dropped: {}".format(gone, out))
            failed = True
    # The legend describes exactly the columns rendered - no entry for a field that no longer
    # arrives, and no rendered column left unexplained.
    legend = slimmed.get("columns") or {}
    headings = [cell.strip() for cell in slimmed["rows"].split("\n")[0].strip("|").split("|")]
    if set(legend) - set(headings):
        print("ERROR: the legend describes columns the table does not have: {}".format(sorted(set(legend) - set(headings))))
        failed = True

    # The semantic content survives untouched.
    for kept in ("state", "show_limit", "soc_percent", "import_rate", "total_cost"):
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

    # rows reaches the model as a markdown table. JSON spells a table out by repeating every column
    # name on every row - on a real 73-row plan that was 19,723 characters of key names, 73% of the
    # payload, and rendering it as a table took the whole response from 41,709 to 12,497.
    table = slimmed.get("rows")
    if not isinstance(table, str):
        print("ERROR: rows did not render as a table: {!r}".format(table))
        return True
    lines = table.split("\n")
    if len(lines) != 3:
        print("ERROR: expected a header, a separator and one data row: {!r}".format(lines))
        failed = True
    if not lines[1].strip().startswith("| ---"):
        print("ERROR: the table has no markdown separator row: {!r}".format(lines[1]))
        failed = True
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    values = [cell.strip() for cell in lines[-1].strip("|").split("|")]

    def column_of(heading):
        """Index of a column by the heading the table shows."""
        return header.index(heading) if heading in header else -1

    # The same headings as the web plan page, so a model and a user looking at their plan are
    # reading the same table. Raw field names would be a second vocabulary for one set of data.
    for heading in ("Time", "Import p", "Export p", "State", "Limit %", "PV kWh (10%)", "Load kWh (10%)", "Clip kWh", "XLoad kWh", "Car kWh", "SoC % (chg kWh)", "Cost \u00a3", "Total \u00a3"):
        if column_of(heading) < 0:
            print("ERROR: the header is missing the plan page's {!r} column: {}".format(heading, header))
            failed = True
    if any("color" in heading for heading in header):
        print("ERROR: a presentation field reached the header: {}".format(header))
        failed = True
    # The point of the whole change: a column name appears once, not once per row.
    if lines[-1].count("Import"):
        print("ERROR: a data row still repeats the column name: {!r}".format(lines[-1]))
        failed = True
    if len(values) != len(header):
        print("ERROR: a data row has {} cells against {} columns".format(len(values), len(header)))
        failed = True
    elif values[column_of("Import p")] != "30.26":
        print("ERROR: a value did not land under its own column: {} / {}".format(header, values))
        failed = True

    # Change and alternative-forecast fields are folded into the column they belong to rather than
    # taking one of their own, and a change carries an explicit sign so it cannot be read as a
    # second measurement.
    for gone in ("soc_change", "cost_change", "pv_forecast10", "load_forecast10"):
        if any(gone in heading for heading in header):
            print("ERROR: {} kept its own column: {}".format(gone, header))
            failed = True
    if values[column_of("SoC % (chg kWh)")] != "85 (-1.41)":
        print("ERROR: the change was not folded into its column: {!r}".format(values[column_of("SoC % (chg kWh)")]))
        failed = True
    if values[column_of("PV kWh (10%)")] != "0.28 (0.19)":
        print("ERROR: the 10% forecast was not folded into the PV column: {!r}".format(values[column_of("PV kWh (10%)")]))
        failed = True
    # A rise must be signed too, or "84 (0.4)" reads as an unrelated second number.
    rising, _ = format_plan_rows_table([{"soc_percent": 84, "soc_change": 0.4}])
    if "84 (+0.4)" not in rising:
        print("ERROR: a positive change was not signed: {!r}".format(rising))
        failed = True
    # A change with no column to fold into keeps its own rather than disappearing.
    orphan, _ = format_plan_rows_table([{"time": "Fri 09:00", "soc_change": 0.4}])
    if "soc_change" not in orphan.split("\n")[0]:
        print("ERROR: a change with no matching column was dropped: {!r}".format(orphan))
        failed = True

    # iBoost and carbon get columns exactly when they are enabled, which is the same rule the plan
    # page follows: output.py only writes iboost/iboost_change into a row under `if
    # self.iboost_enable`, and co2_rate/co2_total under `if self.carbon_enable`, so presence in the
    # data IS the feature being on. An install with neither should see neither column rather than
    # two of empty cells.
    enabled, _ = format_plan_rows_table([{"time": "Fri 09:00", "iboost": 1.2, "iboost_change": 0.3, "co2_rate": 210, "co2_total": 4.1}])
    enabled_header = enabled.split("\n")[0]
    for heading in ("iBoost kWh", "CO2 g/kWh", "CO2 kg"):
        if heading not in enabled_header:
            print("ERROR: {} is missing when the feature is enabled: {!r}".format(heading, enabled_header))
            failed = True
    if "1.2 (+0.3)" not in enabled:
        print("ERROR: the iBoost slot figure was not folded into its column: {!r}".format(enabled))
        failed = True
    if any(heading in header for heading in ("iBoost kWh", "CO2 g/kWh", "CO2 kg")):
        print("ERROR: a disabled feature still took a column: {}".format(header))
        failed = True

    # A field Predbat adds later still reaches the model, under its own name, rather than being
    # silently swallowed because nobody remembered to add it to PLAN_COLUMNS.
    unknown, _ = format_plan_rows_table([{"time": "Fri 09:00", "brand_new_field": 7}])
    if "brand_new_field" not in unknown.split("\n")[0]:
        print("ERROR: a field not in the column table was dropped: {!r}".format(unknown))
        failed = True

    # An empty cell has to mean something specific, or a model reads it as zero - "no car charging
    # figure for this slot" and "the car drew nothing" are different claims.
    if "not set" not in (slimmed.get("rows_format") or ""):
        print("ERROR: nothing tells the model what an empty cell means: {}".format(slimmed.get("rows_format")))
        failed = True

    # A pipe inside a value would end its cell early and shift every value after it one column
    # left - silent corruption, not a visible error.
    piped, _ = format_plan_rows_table([{"state": "a|b", "import_rate": 1}])
    if "a\\|b" not in piped:
        print("ERROR: a pipe in a value was not escaped: {!r}".format(piped))
        failed = True
    # Split on unescaped pipes only - a markdown renderer reads "\\|" as a literal, so a naive
    # split counts the escape itself as a column boundary and reports a break that is not there.
    piped_cells = re.split(r"(?<!\\)\|", piped.split("\n")[-1].strip("|"))
    if len(piped_cells) != 2:
        print("ERROR: a pipe in a value broke its row into extra columns: {!r} -> {}".format(piped, piped_cells))
        failed = True

    # A field only some slots carry still gets a column, with an empty cell where it does not.
    sparse, _ = format_plan_rows_table([{"time": "Fri 09:00"}, {"time": "Fri 09:30", "car_rate": 6.9}])
    if "Car rate" not in sparse.split("\n")[0]:
        print("ERROR: a field present on only some rows lost its column: {!r}".format(sparse))
        failed = True

    # The legend is keyed by the heading the table actually shows. Keyed by field name - which it
    # was - it would explain "pv_forecast" to a model looking at a column called "PV kWh (10%)".
    legend = slimmed.get("columns") or {}
    if "pv_forecast" in legend or "PV kWh (10%)" not in legend:
        print("ERROR: the legend is not keyed by the heading the table shows: {}".format(sorted(legend)))
        failed = True
    if "10%" not in legend.get("PV kWh (10%)", ""):
        print("ERROR: nothing explains what the bracketed PV figure is: {}".format(legend.get("PV kWh (10%)")))
        failed = True
    if "change" not in legend.get("SoC % (chg kWh)", ""):
        print("ERROR: nothing explains what the bracketed SoC figure is: {}".format(legend.get("SoC % (chg kWh)")))
        failed = True

    # The trap this exists for: output.py divides cost by 100 before publishing, so cost is in the
    # MAJOR currency unit while the rates beside it are in the minor one. A model that assumes
    # both are pence is out by a factor of 100 on every cost it quotes. The heading is where that
    # is now said, which is the one place it cannot be missed.
    pounds = slimmed["rows"].split("\n")[0]
    if "Total \u00a3" not in pounds or "Import p" not in pounds:
        print("ERROR: the headings do not distinguish the major and minor currency units: {!r}".format(pounds))
        failed = True

    # Symbols come from the plan, so a non-GBP install is not told everything is in pounds.
    euros = slim_plan(dict(plan, currency_symbols=["\u20ac", "c"]))["rows"].split("\n")[0]
    if "Total \u20ac" not in euros or "Import c" not in euros:
        print("ERROR: the headings ignored the plan's own currency symbols: {!r}".format(euros))
        failed = True

    # It must describe only what is there. This plan has no iBoost or carbon columns, and a legend
    # for absent columns would invite the model to ask about data that does not exist.
    for absent in ("iBoost kWh", "CO2 g/kWh", "CO2 kg"):
        if absent in legend:
            print("ERROR: the legend documents {}, which this plan does not contain: {}".format(absent, sorted(legend)))
            failed = True
    # An unparseable time is passed through rather than guessed at.
    passthrough_rows = slim_plan_rows([{"time": "not a timestamp", "state": "Demand"}])
    passthrough = {"rows": passthrough_rows}
    if passthrough["rows"][0]["time"] != "not a timestamp":
        print("ERROR: an unparseable time was not passed through: {}".format(passthrough))
        failed = True

    return failed


def test_set_config_refuses_what_it_cannot_change(my_predbat):
    """set_config validates its target instead of reporting success for a write that vanished.

    set_state_external() matches on the full entity id and simply returns when it matches nothing:
    a bare name has no domain either, so it falls through every branch and does nothing at all.
    set_config called it and reported "updated successfully" regardless - so a model handed an
    apps.yaml key told the user the change was made, and the user believed it. That is the exact
    failure seen with car_charging_exclusive, which is an APPS_SCHEMA key, not an entity.

    Mutation checks: dropping the find_config_item guard, or the apps.yaml hint, fails below.
    """
    failed = False
    print("**** Testing set_config refuses what it cannot change ****")

    original_args = my_predbat.args
    try:
        my_predbat.args = dict(original_args or {})
        my_predbat.args["car_charging_exclusive"] = [True]
        tools = PredbatTools(my_predbat)

        # An apps.yaml key is refused, and the refusal names the tool that owns it.
        result = asyncio.run(tools.execute("set_config", {"entity_id": "car_charging_exclusive", "value": "False"}))
        if result.get("success"):
            print("ERROR: set_config claimed success for an apps.yaml key: {}".format(result))
            failed = True
        if "set_apps_config" not in str(result.get("error", "")):
            print("ERROR: the refusal does not point at the right tool: {}".format(result))
            failed = True

        # An outright unknown name is refused too, rather than silently doing nothing.
        result = asyncio.run(tools.execute("set_config", {"entity_id": "no_such_setting_at_all", "value": 1}))
        if result.get("success"):
            print("ERROR: set_config claimed success for an unknown name: {}".format(result))
            failed = True

        # A real config item resolves by bare name as well as by entity id - a model has no way to
        # know the entity prefix and will reasonably try the short form.
        item = next((entry for entry in my_predbat.CONFIG_ITEMS if entry.get("type") == "switch" and entry.get("entity")), None)
        if item is None:
            print("ERROR: no switch config item to test against")
            return True
        if tools.find_config_item(item["name"]) is not item:
            print("ERROR: a bare setting name does not resolve to its config item")
            failed = True
        if tools.find_config_item(item["entity"]) is not item:
            print("ERROR: a full entity id does not resolve to its config item")
            failed = True
    finally:
        my_predbat.args = original_args
    return failed


def test_set_config_coerces_switch_value(my_predbat):
    """set_config must coerce its string 'value' before writing, not hand it to set_state_external as-is.

    The tool schema declares 'value' as a JSON string unconditionally, because a model has no way
    to know a setting's real type in advance - so a request to turn a switch off arrives as the
    text "False". set_state_external() picks turn_on/turn_off by Python truthiness, and any
    non-empty string, including "False", is truthy: an already-on switch asked to turn off via
    set_config previously stayed on and set_config still reported success, since it only checked
    that the entity existed, never that the value actually moved.

    Mutation checks: dropping _coerce_config_value, or the applied-vs-requested check that follows
    it, fails below.
    """
    failed = False
    print("**** Testing set_config coerces switch value ****")

    item = next((entry for entry in my_predbat.CONFIG_ITEMS if entry.get("type") == "switch" and entry.get("entity")), None)
    if item is None:
        print("ERROR: no switch config item to test against")
        return True

    original_value = item.get("value")
    # switch_event() (userinterface.py) passes every CONFIG_ITEMS switch change through
    # self.components.switch_event() before applying it - real code always has a Components
    # registry by the time a turn can run, but create_predbat() never sets one up, since nothing
    # else in this file needs it. An empty registry behaves as a no-op router, which is exactly
    # what this test wants.
    original_components = getattr(my_predbat, "components", None)
    my_predbat.components = Components(my_predbat)
    tools = PredbatTools(my_predbat)
    try:
        # An on switch asked for the string "False" must actually turn off.
        item["value"] = True
        result = asyncio.run(tools.execute("set_config", {"entity_id": item["name"], "value": "False"}))
        if not result.get("success"):
            print("ERROR: set_config failed to turn off a switch given the string 'False': {}".format(result))
            failed = True
        if item.get("value") is not False:
            print("ERROR: switch value is {!r} after setting 'False', expected False".format(item.get("value")))
            failed = True
        if (result.get("data") or {}).get("new_value") is not False:
            print("ERROR: set_config reported new_value {!r}, expected False".format((result.get("data") or {}).get("new_value")))
            failed = True

        # And the reverse: an off switch asked for the string "True" must turn on.
        item["value"] = False
        result = asyncio.run(tools.execute("set_config", {"entity_id": item["name"], "value": "True"}))
        if not result.get("success") or item.get("value") is not True:
            print("ERROR: set_config failed to turn on a switch given the string 'True': {}".format(result))
            failed = True

        # A value that is not a recognisable boolean is refused up front, not silently misread.
        item["value"] = True
        result = asyncio.run(tools.execute("set_config", {"entity_id": item["name"], "value": "banana"}))
        if result.get("success"):
            print("ERROR: set_config accepted an invalid switch value 'banana': {}".format(result))
            failed = True
        if item.get("value") is not True:
            print("ERROR: an invalid value changed the switch anyway: {}".format(item.get("value")))
            failed = True
    finally:
        item["value"] = original_value
        my_predbat.components = original_components
    return failed


def test_set_config_rejects_fractional_value_for_step_one(my_predbat):
    """set_config for a step-1 input_number accepts whole numbers and rejects fractional ones.

    Flooring a fractional value (int(2.9) == 2) would silently write something other than what
    was actually requested while still reporting success against the read-back - the same class
    of bug as the switch truthiness issue, just for numbers. Rejecting up front means the caller
    is told its value was not a whole number, rather than being told, incorrectly, that 2.9 was
    accepted.

    Mutation checks: dropping the is_integer() guard, or replacing the rejection with int()
    flooring, fails below.
    """
    failed = False
    print("**** Testing set_config accepts whole numbers and rejects fractional ones for step-1 settings ****")

    item = next(
        (entry for entry in my_predbat.CONFIG_ITEMS if entry.get("type") in ("input_number", "number") and entry.get("entity") and entry.get("step", 1) == 1 and not entry.get("enable") and not entry.get("enable_condition")),
        None,
    )
    if item is None:
        print("ERROR: no step-1 input_number config item to test against")
        return True

    original_value = item.get("value")
    original_components = getattr(my_predbat, "components", None)
    my_predbat.components = Components(my_predbat)
    tools = PredbatTools(my_predbat)
    try:
        # A whole number, even spelled with a decimal point, is accepted and stored as an int.
        result = asyncio.run(tools.execute("set_config", {"entity_id": item["name"], "value": "5.0"}))
        if not result.get("success"):
            print("ERROR: set_config rejected a whole-number value '5.0': {}".format(result))
            failed = True
        if item.get("value") != 5 or not isinstance(item.get("value"), int):
            print("ERROR: config value is {!r} after setting '5.0', expected int 5".format(item.get("value")))
            failed = True

        # A fractional value is refused rather than silently floored to the wrong number.
        result = asyncio.run(tools.execute("set_config", {"entity_id": item["name"], "value": "2.9"}))
        if result.get("success"):
            print("ERROR: set_config silently accepted a fractional value for a step-1 setting: {}".format(result))
            failed = True
        if item.get("value") != 5:
            print("ERROR: a rejected fractional value changed the setting anyway: {}".format(item.get("value")))
            failed = True
    finally:
        item["value"] = original_value
        my_predbat.components = original_components
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
    failed |= test_args_from_apps_yaml_snapshot_is_redacted(my_predbat)
    failed |= test_registry_secret_flags_drive_redaction(my_predbat)
    failed |= test_get_apps_config_paths(my_predbat)
    failed |= test_get_plan_is_slimmed(my_predbat)
    failed |= test_set_config_refuses_what_it_cannot_change(my_predbat)
    failed |= test_set_config_coerces_switch_value(my_predbat)
    failed |= test_set_config_rejects_fractional_value_for_step_one(my_predbat)
    failed |= test_pathological_regex_arguments_are_rejected(my_predbat)
    failed |= test_get_entity_history_does_not_block_the_event_loop(my_predbat)
    failed |= test_parse_log_time_bound_accepts_date_time_or_both(my_predbat)
    failed |= test_parse_log_time_bound_rejects_junk(my_predbat)
    return failed


def test_parse_log_time_bound_accepts_date_time_or_both(my_predbat):
    """parse_log_time_bound() accepts a bare date, a bare time, or both, and defaults the missing half."""
    print("**** Testing parse_log_time_bound accepts date, time or both ****")
    failed = False

    today = datetime.now().date()

    cases = [
        # (value, end_of_range, expected datetime)
        ("2026-08-28", False, datetime(2026, 8, 28, 0, 0, 0)),
        ("2026-08-28", True, datetime(2026, 8, 28, 23, 59, 59, 999999)),
        ("2026-08-28 17:00", False, datetime(2026, 8, 28, 17, 0, 0)),
        ("2026-08-28 17:00:30", False, datetime(2026, 8, 28, 17, 0, 30)),
        # A bare time means that time today, whichever bound it is
        ("17:00", False, datetime(today.year, today.month, today.day, 17, 0, 0)),
        ("17:00", True, datetime(today.year, today.month, today.day, 17, 0, 0)),
        ("17:00:30", False, datetime(today.year, today.month, today.day, 17, 0, 30)),
    ]

    for value, end_of_range, expected in cases:
        got = parse_log_time_bound(value, "start", end_of_range=end_of_range)
        if got != expected:
            print("ERROR: parse_log_time_bound({!r}, end_of_range={}) returned {}, expected {}".format(value, end_of_range, got, expected))
            failed = True

    # No bound given is not an error - it just means "unbounded"
    for empty in (None, ""):
        if parse_log_time_bound(empty, "start") is not None:
            print("ERROR: parse_log_time_bound({!r}) should return None".format(empty))
            failed = True

    if not failed:
        print("✓ Test passed: parse_log_time_bound accepts date, time or both")
    return failed


def test_parse_log_time_bound_rejects_junk(my_predbat):
    """parse_log_time_bound() raises a named MCPArgumentError rather than a bare traceback."""
    print("**** Testing parse_log_time_bound rejects junk ****")
    failed = False

    for value in ("not-a-date", "2026-13-45", "25:99", 12345, "2026-08-28T17:00:00Z"):
        try:
            parse_log_time_bound(value, "start")
            print("ERROR: parse_log_time_bound({!r}) should have raised MCPArgumentError".format(value))
            failed = True
        except MCPArgumentError as e:
            if "start" not in str(e):
                print("ERROR: MCPArgumentError for {!r} should name the argument, got {}".format(value, e))
                failed = True
        except Exception as e:
            print("ERROR: parse_log_time_bound({!r}) raised {} not MCPArgumentError".format(value, type(e).__name__))
            failed = True

    if not failed:
        print("✓ Test passed: parse_log_time_bound rejects junk with a named error")
    return failed
