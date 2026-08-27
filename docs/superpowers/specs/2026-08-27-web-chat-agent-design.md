# Predbat Web Chat Agent — Design

Date: 2026-08-27
Status: Awaiting review

## 1. Purpose

Add a **Chat** tab to the Predbat web interface, backed by an LLM served through OpenRouter,
that can answer questions about the user's own system and change its settings. The agent is
given Predbat's existing tool set directly as function-calling tools, so it can read the plan,
the log, the live configuration, `apps.yaml` and Predbat's internal state, and — behind a
confirmation gate — change a setting or override a plan slot.

The feature exists so a user can ask *"why is it charging at 3am?"* or *"my export slot
vanished, what happened?"* and get an answer grounded in their actual data, without having to
export a debug YAML, open an issue, or wire up an external MCP client.

Scope for this first implementation:

- A new `chat` component, enabled only when OpenRouter credentials and a model are configured
  in `apps.yaml`.
- A shared tool layer, extracted from the MCP server so both surfaces call one implementation.
- A single shared conversation, persisted through the Storage component.
- Server-sent-event streaming of tokens, tool calls and confirmations to the browser.
- A write-confirmation gate, on by default, controlled by a Predbat switch.
- A model picker in the tab, defaulting to the `apps.yaml` model.

Explicitly out of scope, listed in section 16.

## 2. Why not drive the existing MCP server

The chat agent does **not** speak MCP. It presents the tools to the model directly in
OpenAI function-calling form and executes them in-process.

Speaking MCP would mean the chat component acting as an HTTP MCP client against Predbat's own
MCP server: a second port, an OAuth or bearer handshake against ourselves, a JSON-RPC envelope
in each direction, and a hard dependency on `mcp_enable` being switched on. All of that to
reach Python methods that are already in the same process. The protocol buys nothing here —
its value is crossing a process boundary, and there is no boundary to cross.

The MCP tool set is also much richer than the real REST API, which is only `/api/state` and
`/api/service` (`docs/rest_api.md`). Routing chat through HTTP endpoints would mean first
building seven new REST endpoints to reach parity. The tools are therefore extracted into a
shared Python layer that both the MCP server and the chat agent project into their own schema
dialect.

## 3. The constraint that shapes everything: one thread per component

`Hass.create_task()` (`apps/predbat/hass.py:223`) starts a **new thread** per component, each
running its own `asyncio.run()`. Components therefore do not share an event loop.

The existing house pattern, seen in `WebInterface.html_component_restart`
(`apps/predbat/web.py:4866`), is for a web handler to `await` another component's coroutine
directly — which executes that coroutine on the *web* component's loop, not the owning
component's. This works only because those coroutines hold no loop-bound state.

**The chat component must obey the same rule.** Concretely:

- `aiohttp.ClientSession` is created per request inside an `async with`, as `axle.py:250` does.
  No session is cached on the component.
- Shared state (message list, event buffer, pending confirmations) uses plain lists/dicts
  guarded by a `threading.Lock`, never `asyncio.Queue`, `asyncio.Event` or `asyncio.Lock`.
- The confirmation gate is polled, not awaited on a synchronisation primitive: the parked
  turn loops on `await asyncio.sleep(0.2)` reading `pending_confirm` under the lock. This
  needs no thread and no loop-bound object, and matches the cursor polling the SSE handler
  already uses.

The chat component's own thread does only housekeeping: periodic persistence flush, history
pruning and the health timestamp. The agentic turn itself runs on whichever loop invoked it,
which in practice is the web server's.

This is a deliberate departure from how a component would normally own its work, and it is the
reason the design is testable without a web server: `ChatAgent.run_turn()` is an ordinary
coroutine that can be awaited from a test.

## 4. Architecture

Three new files, one refactor.

| File | Contents |
| ---- | -------- |
| `apps/predbat/agent_tools.py` | `PredbatTools` class and `TOOL_DEFS`; the single implementation of all nine tools |
| `apps/predbat/chat.py` | `ChatAgent(ComponentBase)`; OpenRouter client, agentic loop, conversation store, confirmation gate |
| `apps/predbat/web_chat.py` | `WebChat`; the Chat tab page and its routes |
| `apps/predbat/web_mcp.py` | *refactored*: `MCPServerWrapper(PredbatTools)`, keeping only the protocol envelope |

```text
browser ──HTTP/SSE──▶ web_chat.WebChat ──await──▶ chat.ChatAgent ──HTTPS──▶ OpenRouter
                                                        │
                                                        └──▶ agent_tools.PredbatTools ──▶ base (PredBat)
                                                                      ▲
                                        MCP client ──JSON-RPC──▶ web_mcp.MCPServerWrapper
```

## 5. The shared tool layer

### 5.1 `PredbatTools`

`PredbatTools` takes `(base, log_func)` and holds the nine `_execute_*` coroutines lifted
verbatim out of `MCPServerWrapper`, together with the module-level helpers they use
(`json_safe_value`, `summarise_state_value`, `measure_state_value`, `parse_number_argument`,
`compile_filter_argument`, `parse_bool_argument`, `MCPArgumentError`) and their constants
(`LOG_FILTER_TYPES`, `MCP_LOG_*`, `MCP_STATE_*`).

The constants keep their `MCP_` prefix. Renaming them would churn
`apps/predbat/tests/test_web_mcp.py` for no benefit, and they describe the tool contract rather
than the protocol.

`web_mcp.py` re-exports every moved name (`from agent_tools import PredbatTools, LOG_FILTER_TYPES, ...`)
so existing imports in tests and elsewhere continue to resolve.

`MCPServerWrapper` becomes `class MCPServerWrapper(PredbatTools)` and keeps only OAuth, the
JSON-RPC envelope, `_handle_initialize`, `_handle_tools_list` and `_handle_tools_call`. Because
it inherits the handlers, existing tests that call `mcp._execute_get_log(...)` keep working
untouched.

### 5.2 `TOOL_DEFS`

A module-level list, one entry per tool, in the order the MCP server lists them today:

```python
TOOL_DEFS = [
    {
        "name": "get_apps",
        "description": "Get predbat apps.yaml static configuration data, ...",
        "parameters": {"type": "object", "properties": {...}, "required": []},
        "writes": False,
        "chat_omit_properties": ["masked"],
    },
    ...
]
```

| Field | Meaning |
| ----- | ------- |
| `name`, `description`, `parameters` | Exactly the values the MCP server publishes today |
| `writes` | `True` for `set_config` and `set_plan_override`, `False` for the other seven |
| `chat_omit_properties` | Properties removed from the *chat* projection only (section 12.1) |

Two projections and one dispatcher:

- `mcp_tool_list()` → `[{"name", "description", "inputSchema": parameters}]`
- `openai_tool_list()` → `[{"type": "function", "function": {"name", "description", "parameters"}}]`,
  with `chat_omit_properties` stripped from `properties`
- `async execute(name, arguments)` → the tool's result dict, or
  `{"success": False, "error": "Unknown tool: ..."}`

`_handle_tools_list` becomes `return {"tools": mcp_tool_list()}` and `_handle_tools_call`
becomes `execute()` plus the MCP content envelope. A golden test asserts `mcp_tool_list()` is
identical to the list `web_mcp.py` publishes today, so the refactor cannot silently change the
MCP contract.

## 6. Component registration and gating

Added to `COMPONENT_LIST` in `apps/predbat/components.py`, phase 1, restartable:

```python
"chat": {
    "class": ChatAgent,
    "name": "AI Chat Agent",
    "can_restart": True,
    "phase": 1,
    "args": {
        "api_key":        {"required": True,  "config": "openrouter_api_key"},
        "model":          {"required": True,  "config": "openrouter_model"},
        "base_url":       {"required": False, "config": "openrouter_base_url",
                           "default": "https://openrouter.ai/api/v1"},
        "max_tool_calls": {"required": False, "config": "openrouter_max_tool_calls", "default": 8},
        "max_history":    {"required": False, "config": "openrouter_max_history", "default": 40},
        "max_tokens":     {"required": False, "config": "openrouter_max_tokens", "default": 0},
        "turn_timeout":   {"required": False, "config": "openrouter_turn_timeout", "default": 180},
    },
},
```

`required: True` on the key and model gives the requested gating with no extra code: absent
either one, `Components.initialize()` never constructs the component. It already logs a
targeted warning when a component is *partially* configured
(`apps/predbat/components.py:75-82`), so setting only `openrouter_api_key` produces
`Warn: Skipping AI Chat Agent interface, missing required configuration: openrouter_model`
rather than silence.

`ChatAgent.run()` returns `True` on its first tick without any network I/O. Credentials are
validated lazily on the first turn. Validating at startup would let a slow or unreachable
OpenRouter block Predbat's boot inside `wait_api_started()` for up to ten minutes.

`run()` thereafter, once a minute: flush a dirty conversation to storage, prune history, and
`update_success_timestamp()` so the Components tab shows the component as healthy.

## 7. Configuration

### 7.1 `apps.yaml`

All added to `APPS_SCHEMA` in `apps/predbat/config.py`:

| Key | Type | Required | Default | Purpose |
| --- | ---- | -------- | ------- | ------- |
| `openrouter_api_key` | string | Yes | — | OpenRouter API key |
| `openrouter_model` | string | Yes | — | Default model id, e.g. `anthropic/claude-sonnet-4.5` |
| `openrouter_base_url` | string | No | `https://openrouter.ai/api/v1` | Override for any OpenAI-compatible endpoint (Ollama, LiteLLM, a proxy) |
| `openrouter_max_tool_calls` | integer | No | 8 | Tool calls allowed in one turn before the loop stops |
| `openrouter_max_history` | integer | No | 40 | Messages retained in the conversation |
| `openrouter_max_tokens` | integer | No | 0 (unset) | Per-response completion cap; omitted from the request when 0 |
| `openrouter_turn_timeout` | integer | No | 180 | Wall-clock seconds for one turn, excluding time spent awaiting a confirmation |

`openrouter_api_key` matches the existing `_key` suffix heuristic, so it is already redacted by
`mask_secret_args()` in the Apps view, the debug YAML and the `get_apps` tool. No new redaction
rule is needed; a test asserts this.

`openrouter_base_url` is documented rather than hidden. It is the only supported way to point
Predbat at a local model, and an undocumented key that users discover from the source is worse
than a documented one.

### 7.2 `CONFIG_ITEMS`

One new switch in `apps/predbat/config.py`:

```python
{
    "name": "chat_confirm_writes",
    "friendly_name": "Chat confirm before changing settings",
    "type": "switch",
    "default": True,
},
```

Surfaces as `switch.predbat_chat_confirm_writes` in Home Assistant and in the Config tab. It is
read at the moment a write tool is called, not cached at turn start, so toggling it takes
effect immediately.

## 8. Conversation model and persistence

One conversation, shared by everyone who opens the tab. A Predbat instance serves one
household; separate per-browser conversations would fragment context without anyone asking for
it, and would make a mid-turn reload lose the turn.

State held on the component under `self.lock`:

| Attribute | Contents |
| --------- | -------- |
| `messages` | OpenAI-format list: `user`, `assistant` (with `tool_calls`), `tool` |
| `events` | Append-only event buffer, each stamped with a monotonic `seq` |
| `event_seq` | Next sequence number |
| `active_turn` | Turn id of the running turn, or `None` |
| `pending_confirm` | `call_id` → `{"approved": bool or None, "expires": datetime}` |
| `selected_model` | Currently chosen model id; defaults to `openrouter_model` |
| `usage_total` | Running `{prompt_tokens, completion_tokens, cost}` for the conversation |

The system message is **not** stored — it is rebuilt from live data at the start of every turn
(section 10), so a restored conversation is never anchored to a stale snapshot.

### 8.1 Persistence

Through the Storage component, as CLAUDE.md requires:

```python
await self.storage.save("chat", "conversation", payload, format="json", expiry=...)
```

Payload: `{"version": 1, "model": selected_model, "messages": [...], "usage_total": {...}, "updated": iso8601}`.

Written at the end of each turn and on the housekeeping tick when dirty — never per token.
Loaded on the first `run()`; a payload with an unrecognised `version` is discarded and logged
rather than half-parsed.

### 8.2 History trimming — a trap worth stating

Trimming to "the last N messages" will eventually cut between an `assistant` message carrying
`tool_calls` and the `tool` messages answering them. OpenAI-compatible APIs reject that with a
400: every `tool_call_id` must have a matching `tool` message in the same request.

Trimming therefore walks backwards to the nearest **`user` message boundary** at or before the
cut point, so a kept window always begins a complete exchange. If no such boundary exists
within the window the whole conversation is kept and a warning is logged.

## 9. The agentic turn

```text
run_turn(text):
    claim active_turn, else raise Busy
    append user message + emit "user" event
    for iteration in range(max_tool_calls + 1):
        messages = [system_snapshot()] + trim(self.messages)
        stream POST {base_url}/chat/completions
              {model, messages, tools: openai_tool_list(),
               stream: true, usage: {include: true}, [max_tokens]}
        for each SSE chunk:
            content delta      -> emit "delta"
            tool_call delta    -> accumulate by index
            usage              -> emit "usage", add to usage_total
        append assistant message; emit "assistant"
        if no tool_calls: break
        for each tool_call:
            if TOOL_DEFS[name].writes and chat_confirm_writes:
                emit "confirm"; await approval (section 11)
                if not approved: result = {"success": false, "error": "User declined"}
            else:
                emit "tool_start"; result = await tools.execute(name, args); emit "tool_end"
            append tool message
    else:
        emit "assistant" with a visible "tool call limit reached" note
    persist; emit "done"; release active_turn
```

Notes:

- Tool-call deltas arrive fragmented across chunks and are keyed by `index`, not by `id` — the
  `id` and `name` appear only in the first fragment. The accumulator keys on `index`.
- Arguments arrive as a JSON string built up across chunks; malformed JSON at the end is
  reported back to the model as a tool error rather than raised, so it can retry.
- The whole turn is wrapped in `asyncio.wait_for(..., turn_timeout)`. Time parked awaiting a
  confirmation is excluded by pausing the deadline, otherwise a user who steps away turns their
  own approval into a timeout.
- `active_turn` is claimed and released under the lock, and released in a `finally`, so a
  crashed turn cannot wedge the tab.

## 10. System prompt and the live snapshot

The system message is rebuilt each turn from two parts.

**A static primer** — a short paragraph on what Predbat is, that the user is the system owner,
that answers should be concise and reference real values, that it should call a tool rather
than guess, and that it must never invent an entity name.

**A live snapshot**, roughly 400-800 tokens, assembled from `self.base`: current time and
timezone, mode, `soc_kw`/`soc_max`/`soc_percent`, reserve, current import and export rate, the
next charge window and next export window with times and limits, `status`, `had_errors`,
inverter count and types, number of cars, and the currency symbol.

The snapshot answers the commonest questions with zero tool calls and stops weaker models
answering from nothing. Everything deeper — the full plan, the log, `apps.yaml`, internal state
— stays behind a tool call, which is what keeps the per-turn cost bounded.

Snapshot assembly is a pure function of `base` state, isolated in `chat.py` as
`build_snapshot(base)` so it can be tested against a stub without an LLM.

## 11. Write confirmation

When `chat_confirm_writes` is on and the model calls a tool with `writes: True`:

1. A `confirm` event is emitted carrying `call_id`, tool name and the decoded arguments.
2. The browser renders a card: *"Set `input_number.predbat_best_soc_keep` to 2.0"* with
   **Approve** and **Reject**.
3. The turn parks, polling `pending_confirm[call_id]["approved"]` every 0.2 s for up to 300 s.
4. `POST /chat/confirm` sets `approved` under the lock.
5. Approved → the tool executes normally. Rejected or timed out → the tool result becomes
   `{"success": false, "error": "User declined this change"}`.

Feeding a rejection back as an ordinary tool result, rather than aborting, lets the model
acknowledge it and offer an alternative — which is the behaviour a user expects from a refusal.

With the switch off, write tools execute directly and still emit `tool_start`/`tool_end`, so the
transcript always records what was changed.

A pending confirmation is dropped when its turn ends, and `POST /chat/confirm` for an unknown or
expired `call_id` returns 404 rather than silently succeeding.

## 12. Security and privacy

### 12.1 Credentials must not reach OpenRouter

Chat output goes to a third party — OpenRouter, and through it whichever provider serves the
chosen model. `get_apps` accepts a `masked` argument that defaults to true but *can be set
false*, which over MCP is the user's own deliberate choice on their own machine. In chat it
would let the model ask for unmasked credentials and ship them off-box.

The chat projection therefore strips `masked` from `get_apps`' schema entirely
(`chat_omit_properties`), so the model cannot express the request. `_execute_get_apps` keeps its
existing default of `masked=True`, so the omitted property resolves to the safe value. A test
asserts the property is absent from `openai_tool_list()` and present in `mcp_tool_list()`.

`get_state` already filters through `DEBUG_EXCLUDE_LIST`, and `get_config`/`get_entities`
return Predbat's own settings, which hold no credentials.

### 12.2 Disclosure

The Chat tab shows a dismissible banner on first use naming the destination — OpenRouter and
the selected model's provider — and stating that tool results, including log lines and
configuration, are sent there. The dismissal is stored in `localStorage`.

### 12.3 Network exposure

The Predbat web UI has no authentication, so anyone who can reach port 5052 can use the chat
and spend the user's OpenRouter credit. This is the same exposure the existing MCP caution
documents, and `docs/components.md` will carry the equivalent warning for chat.

### 12.4 Prompt injection

Tool results are data, and some of it (log lines, entity names, `apps.yaml` comments) is text
the model could be steered by. The only privileged action available to a steered model is a
write, and writes are behind the confirmation gate whose card shows the *actual* tool and
arguments rather than the model's description of them. The `max_tool_calls` cap bounds a
runaway loop.

### 12.5 XSS

Model output is rendered as markdown. The renderer escapes HTML **first**, then applies a small
fixed set of transforms (fenced code, inline code, bold, italic, links, bullet and numbered
lists, line breaks). No `innerHTML` of unescaped text anywhere, and no external markdown
library.

## 13. Web layer

Registered from `WebInterface.start()` via `self._register_chat_routes(app)`, mirroring the
existing `_register_annual_routes()` split so the routes can be asserted against a bare
`aiohttp.Application` in a test without opening a socket. Routes are registered only when
`components.get_component("chat")` is not None.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/chat` | The tab |
| GET | `/chat/history` | Full transcript + `usage_total` + current model, for first paint |
| POST | `/chat/send` | `{message}` → `{turn_id}`; **409** if a turn is already active |
| GET | `/chat/stream?cursor=N` | SSE event stream from sequence `N` |
| POST | `/chat/confirm` | `{turn_id, call_id, approve}` |
| POST | `/chat/cancel` | Abort the active turn |
| POST | `/chat/new` | Clear the conversation |
| GET | `/chat/models` | Model list for the picker |
| POST | `/chat/model` | `{id}` → set the active model |

### 13.1 SSE protocol

Frames are `id: <seq>\nevent: <type>\ndata: <json>\n\n`. The handler loops: read events after
the cursor under the lock, write them, `await asyncio.sleep(0.1)`. A comment heartbeat every
15 s keeps proxies from closing an idle stream.

| Event | Payload |
| ----- | ------- |
| `user` | `{text}` |
| `delta` | `{text}` — assistant token chunk |
| `assistant` | `{text}` — the completed message |
| `tool_start` | `{call_id, name, arguments}` |
| `tool_end` | `{call_id, name, ok, elapsed, preview}` |
| `confirm` | `{call_id, name, arguments}` |
| `confirm_result` | `{call_id, approved}` |
| `usage` | `{prompt_tokens, completion_tokens, cost, conversation_cost}` |
| `error` | `{message}` |
| `done` | `{turn_id}` |
| `reload` | `{}` — the cursor predates the buffer; refetch `/chat/history` |

Cursor replay rather than a live queue means two open browsers both follow the same turn, and a
reload mid-turn resumes from where it left off. The buffer is capped at 2000 events; a cursor
older than the buffer's base gets a `reload` event telling the client to refetch `/chat/history`.

### 13.2 Model picker

`GET /chat/models` fetches `{base_url}/models`, keeps only entries whose
`supported_parameters` contains `tools`, and returns `{id, name, prompt_price, completion_price}`
sorted by id. Cached through `storage.fetch_cached("chat", "models", ..., fresh_minutes=1440)`,
so the picker costs one request a day. If the fetch fails, the list degrades to the single
`apps.yaml` model and the dropdown notes that the catalogue is unavailable.

`POST /chat/model` sets `selected_model` and persists it. Switching mid-conversation is allowed
and appends a visible note to the transcript. A "Use default" entry restores the `apps.yaml`
value. The `apps.yaml` model is always offered even if it is missing from the catalogue, so a
custom `openrouter_base_url` with no `/models` endpoint still works.

### 13.3 The tab

Transcript pane; a growing textarea with Enter to send and Shift+Enter for a newline; a footer
showing the model picker, turn tokens and cost, conversation cost, and **New chat**. Tool calls
render **collapsed** — a single line, *"called `get_plan`"*, with a disclosure triangle
revealing arguments and the JSON result. Confirmation cards render inline and expanded. Dark
mode follows the existing `get_header_html()` mechanism.

Nav: `<a href='./chat'>Chat</a>` added to the menu bar in `web_helper.py`, emitted only when
chat is enabled. `get_header_html()` gains a `chat_enabled=False` keyword argument, set at its
single production call site (`apps/predbat/web.py:1698`) from
`components.get_component("chat")`. The default keeps the existing test call site valid.

## 14. Error handling

| Condition | Behaviour |
| --------- | --------- |
| 401 from OpenRouter | `error` event: "OpenRouter rejected the API key — check `openrouter_api_key`" |
| 402 | Surfaced verbatim; it means the account is out of credit |
| 429 | "Rate limited by OpenRouter, try again shortly"; the turn ends rather than retrying, so a user is never billed for a silent retry storm |
| Timeout / connection error | `error` event naming the endpoint; `count_errors` incremented so the Components tab reflects it |
| Model does not support tools | Detected at selection from the catalogue and refused with an explanation; if it slips through, the missing `tool_calls` simply yields a text answer |
| Tool raises | Caught per call; the exception text becomes the tool result so the model can react |
| Malformed tool arguments | Same path — reported to the model as a tool error |

Every error path ends the turn cleanly, releases `active_turn` and emits `done`.

## 15. Testing

New tests, registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py`.

**`tests/test_agent_tools.py`**

- Every `TOOL_DEFS` name resolves to a handler on `PredbatTools`, and every handler is listed.
- `writes` is `True` for exactly `set_config` and `set_plan_override`.
- `mcp_tool_list()` matches a golden copy of the list `web_mcp.py` publishes today.
- `openai_tool_list()` is well-formed OpenAI function-calling shape.
- `masked` is absent from the chat projection of `get_apps` and present in the MCP projection.
- `execute()` dispatches correctly and returns the unknown-tool error for a bad name.
- `MCPServerWrapper` still exposes the inherited `_execute_*` methods.

**`tests/test_chat.py`** — driving `ChatAgent` against a fake OpenRouter that replays canned SSE
byte streams:

- Component gating: missing key, missing model, both present.
- Plain answer with no tool call.
- One tool call, then an answer.
- Chained tool calls across iterations.
- `max_tool_calls` cap reached → visible note, no infinite loop.
- Tool raising an exception → error surfaces as a tool result, turn completes.
- Malformed tool-call argument JSON.
- Write with confirm on: approved, rejected, and timed out.
- Write with confirm off: executes directly.
- History trimming stops at a `user` boundary and never orphans a `tool` message.
- Persistence round-trip through a fake storage, including an unknown `version`.
- `build_snapshot()` against a stub base, including missing/None fields.
- Second `run_turn()` while one is active raises Busy.
- 401, 429 and timeout responses produce the documented `error` events.

**`tests/test_web_chat.py`**

- Routes register on a bare `aiohttp.Application` when chat is enabled, and do not when it is not.
- The nav link appears only when `chat_enabled=True`.
- `/chat/send` returns 409 while a turn is active.
- SSE framing: `id`/`event`/`data` lines, cursor replay, `reload` when the cursor is too old.
- `/chat/confirm` with an unknown `call_id` returns 404.
- `/chat/models` filters to tool-capable models and always includes the `apps.yaml` model.
- The markdown renderer escapes HTML before transforming (the XSS case).

**Regression:** `./run_all --test web_mcp` must pass unchanged after the extraction.

## 16. Out of scope

- Multiple named conversations or per-user history.
- Authentication for the web UI, or per-user access control on chat.
- Image, file or debug-YAML upload into the conversation.
- The agent calling out to anything other than Predbat's own tools.
- Letting the agent define new tools, or run arbitrary code.
- MCP client mode (the chat agent consuming *external* MCP servers).
- Voice input, or a Home Assistant conversation-agent integration.

## 17. Documentation

- `docs/components.md`: a new "AI Chat Agent (chat)" section — what it does, when to enable,
  the configuration table, the network-exposure and third-party-data cautions, and the tool
  table cross-referenced to the MCP one.
- `docs/web-interface.md`: the Chat tab, the confirmation gate, the model picker.
- `docs/apps-yaml.md`: the seven new keys.
- New words to `.cspell/custom-dictionary-workspace.txt`: `openrouter`, `ollama`, `litellm`
  and anything else the hook flags (the file is auto-sorted on commit, so re-stage after).

## 18. Implementation order

1. Extract `agent_tools.py`; refactor `web_mcp.py`; prove `test_web_mcp` is green and the golden
   tool list is unchanged.
2. `ChatAgent` with config, persistence, snapshot and the agentic loop, against a fake OpenRouter.
3. Confirmation gate and the `chat_confirm_writes` switch.
4. `web_chat.py` routes and SSE.
5. The tab's HTML, CSS and client JS.
6. Model picker.
7. Documentation.

Steps 1 and 2 are independently useful and independently testable; the feature is not visible to
a user until step 4.
