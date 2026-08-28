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

from agent_tools import TOOL_DEFS, PredbatTools, mcp_tool_list, openai_tool_list
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


def run_agent_tools_tests(my_predbat):
    """Run every shared tool layer test, returning True if any of them failed."""
    failed = False
    failed |= test_tool_defs_integrity(my_predbat)
    failed |= test_mcp_tool_list_matches_golden(my_predbat)
    failed |= test_openai_tool_list_shape(my_predbat)
    failed |= test_execute_dispatch(my_predbat)
    failed |= test_mcp_wrapper_still_inherits(my_predbat)
    failed |= test_handler_crash_sets_is_error(my_predbat)
    return failed
