# Predbat Components

This document provides a comprehensive overview of all Predbat components, their purposes, and configuration options.

## Table of Contents

- [Overview](#overview)
- [Component List](#component-list)
    - [Database Manager (db)](#database-manager-db)
    - [Home Assistant Interface (ha)](#home-assistant-interface-ha)
    - [Home Assistant History (ha_history)](#home-assistant-history-ha_history)
    - [Web Interface (web)](#web-interface-web)
    - [MCP Server (mcp)](#mcp-server-mcp)
    - [AI Chat Agent (chat)](#ai-chat-agent-chat)
    - [GivEnergy Cloud Direct (gecloud)](#givenergy-cloud-direct-gecloud)
    - [GivEnergy Cloud Data (gecloud_data)](#givenergy-cloud-data-gecloud_data)
    - [Octopus Energy Direct (octopus)](#octopus-energy-direct-octopus)
    - [Axle Energy VPP (axle)](#axle-energy-vpp-axle)
    - [Ohme Charger (ohme)](#ohme-charger-ohme)
    - [myenergi (myenergi)](#myenergi-myenergi)
    - [Fox ESS API (fox)](#fox-ess-api-fox)
    - [Tesla Powerwall Teslemetry API (teslemetry)](#tesla-powerwall-teslemetry-api-teslemetry)
    - [Enphase API (enphase)](#enphase-api-enphase)
    - [Solax Cloud API (Solax)](#solax-cloud-api-solax)
    - [Solis Cloud API (Solis)](#solis-cloud-api-solis)
    - [Sigenergy Cloud API (Sigenergy)](#sigenergy-cloud-api-sigenergy)
    - [DEYE Cloud API (deye)](#deye-cloud-api-deye)
    - [Sunsynk Cloud API (sunsynk)](#sunsynk-cloud-api-sunsynk)
    - [AlphaESS Cloud API (alphaess)](#alphaess-cloud-api-alphaess)
    - [Alert Feed (alert_feed)](#alert-feed-alert_feed)
    - [Carbon Intensity API (carbon)](#carbon-intensity-api-carbon)
    - [Temperature API (temperature)](#temperature-api-temperature)
    - [Kraken Energy (kraken)](#kraken-energy-kraken)
    - [ML Load Prediction (load_ml)](#ml-load-prediction-load_ml)
- [Managing Components](#managing-components)
    - [Checking Component Status](#checking-component-status)
    - [Restarting Components](#restarting-components)
    - [Editing Component Configuration](#editing-component-configuration)
    - [Component Health](#component-health)

## Overview

Predbat uses a modular component architecture where each component provides specific functionality such as database management, cloud API integration, web interfaces, and energy provider integrations.
Each component can be enabled or disabled independently through your `apps.yaml` configuration file.

## Component List

### Database Manager (db)

**Can be restarted:** No

#### What it does (db)

Stores and manages all historical data for Predbat, including energy usage, sensor values, and system states. This allows Predbat to keep its own database of historical information independent of Home Assistant.

#### When to enable (db)

- You want to retain data longer than Home Assistant keeps or you want to run Predbat without Home Assistant.  Under normal use you should not need to configure DB manager and the component will be disabled.

#### Configuration Options (db)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `db_enable` | Boolean | Yes | - | `db_enable` | Set to `true` to enable the database, `false` to disable |
| `db_days` | Integer | No | 30 | `db_days` | Number of days of historical data to keep in the database |

---

### Home Assistant Interface (ha)

**Can be restarted:** No

#### What it does (ha)

Provides the connection between Predbat and Home Assistant. This is the core communication channel that allows Predbat to read sensor data, control devices, and update its status in Home Assistant.

If you are using Predbat without Home Assistant then this interface layer just talks directly to the Database Manager.

#### When to enable (ha)

This component is always enabled and required for Predbat to function.

#### Configuration Options (ha)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `ha_url` | String | No | `http://supervisor/core` | `ha_url` | Home Assistant API URL (the default is for when using an HA app) |
| `ha_key` | String | No | Auto-detected | `ha_key` | Home Assistant access token (auto-detected when running as app) |
| `db_enable` | Boolean | No | false | `db_enable` | Enable database integration |
| `db_mirror_ha` | Boolean | No | false | `db_mirror_ha` | Copy Home Assistant data into Predbat's database |
| `db_primary` | Boolean | No | false | `db_primary` | Use Predbat's database instead of Home Assistant for the primary data source |

---

### Home Assistant History (ha_history)

**Can be restarted:** No

#### What it does (ha_history)

Retrieves and processes historical sensor data from Home Assistant's database (or from the Predbat database).
This component handles all lookups of past energy usage, battery levels, and other historical information.

#### When to enable (ha_history)

This component is always enabled.

#### Configuration Options (ha_history)

No configuration required. This component automatically uses your Home Assistant connection.

---

### Web Interface (web)

**Can be restarted:** Yes

#### What it does (web)

Provides a built-in web server that lets you view and manage Predbat through your web browser. Access dashboards, view battery plans, check logs, and edit configuration all from an easy-to-use web interface.

#### Configuration Options (web)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `port` | Integer | No | 5052 | `web_port` | Port number for the web server |

#### How to access (web)

If you use Predbat as a Home Assistant app then click 'Open Web UI' from the app or add Predbat Web UI to your side bar.
If you run Predbat outside then you can access it from the port as configured:  `http://homename:5052`

---

### MCP Server (mcp)

**Can be restarted:** Yes

#### What it does (mcp)

Provides a programmatic API that allows AI assistants (like ChatGPT, Claude, or other MCP-compatible tools) to read and control Predbat. This enables you to use natural language commands to check status, adjust settings, or override plans.

#### When to enable (mcp)

- You want to control Predbat through AI assistants
- You're building custom integrations or tools
- You want programmatic access to Predbat data

#### Security note (mcp)

The MCP server requires a secret key for authentication. Keep this secret secure and don't share it publicly.

***CAUTION*** Predbat WebUI does not support https currently, so exposing this MCP port externally to your home network would be unwise.

#### Configuration Options (mcp)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `mcp_enable` | Boolean | Yes | false | `mcp_enable` | Set to `true` to enable the MCP server |
| `mcp_secret` | String | No | `predbat_mcp_secret` | `mcp_secret` | Secret key for authentication - change this! |
| `mcp_port` | Integer | No | 8199 | `mcp_port` | Port number for the MCP server |

#### How to configure your MCP client (mcp)

Below is an example MCP configuration inside VSCode, but it will be similar in Cline/Claude/Cursor etc.

```json
Example usage in VSCode
{
 "servers": {
  "predbat-mcp": {
   "url": "http://homeassistant.local:8199/mcp",
   "type": "http",
   "description": "Predbat Model Context Protocol Server",
   "headers": {
    "Authorization" : "Bearer predbat_mcp_secret",
   },
  }
 },
 "inputs": []
}
```

#### Available commands (mcp)

| Tool | What it returns or does |
| ---- | ----------------------- |
| `get_status` | Current system status - mode, SoC, live power figures |
| `get_plan` | The current battery plan, with forecasts and costs. Returned as a markdown table with the same columns as the plan page, plus a legend describing each one |
| `get_config` | Every Predbat setting, with its current value and its default |
| `get_apps` | Your `apps.yaml` configuration, with credentials redacted |
| `get_apps_config` | The current value of one `apps.yaml` key, with a credential-like value redacted |
| `get_log` | Lines from `predbat.log`, filtered by level, search term, regular expression and time window |
| `get_state` | Predbat's internal state variables - the same data a debug yaml carries |
| `get_entities` | All Predbat entities and their states |
| `search_entities` | Search *every* Home Assistant entity id with a regular expression, not just Predbat's own - requires `switch.predbat_ai_ha_state_enable` |
| `get_entity_state` | The current state (and optionally attributes) of one Home Assistant entity - requires `switch.predbat_ai_ha_state_enable` |
| `get_entity_history` | One Home Assistant entity's history over a time window, bucketed into fixed-width time slots - requires `switch.predbat_ai_ha_state_enable` |
| `set_config` | Change a Predbat setting |
| `set_plan_override` | Override the plan for one 30 minute period |

#### Asking an AI assistant to review your setup (mcp)

`get_config`, `get_apps` and `get_log` together give an assistant everything a bug report
normally has to carry, so you can ask it to look over your setup without opening an issue -
for example *"compare my Predbat settings against their defaults and tell me which changes
look wrong"*, or *"find the warnings in the last 24 hours of my log and explain them"*.

`get_log` takes optional arguments:

| Argument | Description |
| -------- | ----------- |
| `filter` | `all`, `info`, `warnings` (the default) or `errors` |
| `search` | Only return lines containing this text, case-insensitive |
| `pattern` | Only return lines matching this Python regular expression, case-insensitive |
| `hours` | Only return lines written in the last N hours |
| `start` | Only return lines at or after this point in time |
| `end` | Only return lines at or before this point in time |
| `max_lines` | How many lines to return - the most recent matches are the ones kept, but they come back oldest-first (default 200, maximum 5000) |
| `line_number` | Return this one line in full, ignoring every other filter |
| `context` | With `line_number`, also return this many lines either side |

All of these narrow the result together rather than replacing one another, so
`filter: errors` with `pattern: "inverter [12]"` returns only the errors that also mention those
inverters.

`start` and `end` each accept a date, a time, or both:

| Value | Means |
| ----- | ----- |
| `2026-08-28` | As `start`, the beginning of that day; as `end`, the end of it - so `end: 2026-08-28` includes everything that happened that day |
| `17:00` or `17:00:30` | That time today |
| `2026-08-28 17:00` | Exactly that moment |

`hours` still works and can be combined with `start`, in which case the narrower of the two wins.
A line with no timestamp of its own - the second and later lines of a traceback - belongs to the
entry above it, so a multi-line entry is kept or dropped as a whole.

#### Keeping the response small enough to be useful

`max_lines` bounds how many lines come back, not how big they are, and some Predbat log lines are
enormous - a single Octopus GraphQL response is one 20KB line. Left unchecked, a few hundred of
those made a tool result of nearly a megabyte, which overflowed the model's context and cost the
assistant the rest of the conversation.

So the response has three guards. Beyond `max_lines`, any single line longer than about a thousand
characters is cut and marked with how much was left off, and the response as a whole stops at a
total size budget - `truncated_reason` says which of the two ended it. A cut line still tells you
its number, and passing that back as `line_number` returns it in full:

```text
2026-08-30 09:58:11: OctopusAPI: Fetched saving sessions... [+19069 chars, get_log line_number=26793]
```

Line numbers count from the start of the previous (rotated) log through the current one, so they
stay valid as the log grows but not across a rotation.

`get_state` exposes the same internal state a `predbat_debug.yaml` carries, but a variable at a
time rather than as a 5MB file. Called with no arguments it returns every variable small enough
to be worth reading - a few hundred of them, together well under a page - and *describes* the
handful of large ones (the per-minute series such as `load_minutes`, `rate_import` and
`pv_today`) in an `omitted` section giving each one's type, length and value range. Ask for one
of those by name with `keys`, narrow by name with `filter`, or raise the per-variable budget
with `max_bytes`.

| Argument | Description |
| -------- | ----------- |
| `keys` | Specific variable names to return (omit for every small variable) |
| `filter` | Only return variables whose name matches this Python regex |
| `max_bytes` | Per-variable size budget before a value is described instead of returned (default 2048, maximum 262144) |

`get_state` and `get_apps` both redact credentials. `get_apps` replaces credential-like values
with `xxx`, so your API keys are not sent to your AI provider; pass `masked: false` if you
deliberately want the raw values. A value counts as a credential if its name contains `_key`,
`password`, `secret` or `token`, **or** if the component registry flags it explicitly - which
covers the credentials a name alone cannot reveal, such as your Octopus account number, a Kraken
MPAN, a site or plant id, and login identifiers like `deye_username`, `kraken_email` and
`myenergi_hub_serial` (the myenergi API's digest-auth username). Inverter serial numbers are
deliberately *not* redacted: they identify hardware rather than authenticate it, and they are
what makes an integration bug report diagnosable. `get_state` applies the same rule *and* the debug yaml's exclusion list, so it can never
return anything a debug dump would not - credentials, the Home Assistant interface, loaded
secrets and the URL caches are not reachable through it at all. `get_apps_config` uses the same
credential check as `get_apps`, but with no `masked: false` escape hatch at all - there is no
legitimate reason for an assistant to ever see one raw credential value, so it is simply never
offered.

`get_apps_config` and `set_apps_config` let an assistant read, and change, one `apps.yaml` setting
at a time - either a top-level key or, using a dotted path such as `forecast_solar[0].azimuth`,
one value inside a nested structure. Both halves accept the same paths, so the model can read back
what it just wrote, and a path that does not exist is answered with the keys that do - see [`set_apps_config`](#available-tools-chat) below for the write side, which is
chat-only.

#### Reading Home Assistant state (mcp)

Every other tool above reads *Predbat's own* entities and configuration. `search_entities`,
`get_entity_state` and `get_entity_history` are different: they can read **any** entity in your
Home Assistant install - presence sensors, device names, anything else in the state machine -
not just the ones Predbat publishes itself. That is a materially larger disclosure to whichever
third-party model is asking than everything else on this page, so all three are off by default
and gated behind `switch.predbat_ai_ha_state_enable`. With the switch off, each tool still
appears in the tool list but returns a clean failure naming the switch, rather than vanishing -
so an assistant can tell you which switch to turn on instead of just failing silently. The
switch is prefixed `ai_`, not `chat_`: it controls this for MCP as well as the Chat tab, since an
MCP client is no less a third party than a chat model is.

`search_entities` returns only `entity_id`, `state` and `last_changed` - never attributes, which
can be bulky on a large install - capped at `limit` matches (default 50, maximum 200) with the
true match count reported separately so a capped result is visibly capped.

`get_entity_history` fetches one entity's history between `start` and `end` (ISO-8601
timestamps, assumed UTC if no offset is given) and aggregates it into `bucket_minutes`-wide
buckets (default 30). The lookback is capped at 30 days before `end`, and the bucket count is
capped at 500 by pulling `end` in rather than widening the buckets - both are reported in the
response (`lookback_clamped`, `range_truncated`) when they bite. Pass `attribute` to bucket a
named attribute instead of the entity's state. Numeric data (including `on`/`off`/`true`/`false`,
which map to 1/0) is bucketed as `min`/`max`/`mean`/`count`/`unavailable`; anything else is
bucketed as `first`/`last`/`changes`, since a most-common value would hide a sensor that flipped
once and stayed there. Which mode applies is decided once per request and reported as `mode`.

---

### AI Chat Agent (chat)

**Can be restarted:** Yes

#### What it does (chat)

Adds a Chat tab to the web interface, backed by a large language model served through
[OpenRouter](https://openrouter.ai). The model can call the same read-only tools the MCP server
exposes, plus a handful of chat-only tools for searching Predbat's documentation and its own
installed source code, and can propose the same two write actions MCP has - each one held for
your approval before it runs when `switch.predbat_chat_confirm_writes` is on (the default). See
[the Chat tab](web-interface.md#chat-view) for how to use it day to day.

Each conversation's system prompt (your Predbat snapshot plus the agent's instructions) is
captured once, when the conversation starts, and replayed byte-for-byte on every later turn rather
than being rebuilt from live state each time. It is also sent with an explicit prompt-caching
breakpoint. Together this means a provider that supports prompt caching only charges full price
for that prompt on the first turn - later turns charge a fraction of it, since the cached portion
is billed at a steep discount rather than as fresh input. The first turn itself costs slightly
*more* than it otherwise would, because writing the prompt into the cache is itself a billed
operation. A provider with no prompt-caching support simply ignores the breakpoint and is
unaffected either way. Measured against `anthropic/claude-sonnet-5` on OpenRouter, with a roughly
2,600-token system prompt: an uncached turn cost $0.005282, and a turn that hit the cache cost
$0.000580 - about a ninth as much. Treat those figures as an illustration of the mechanism, not a
guarantee - the actual saving depends on the model, the provider and the size of your own prompt.

#### When to enable (chat)

- You want a conversational way to ask about your Predbat setup, without running an MCP client
- You are comfortable with log lines and configuration being sent to a third-party AI provider
- You have (or are willing to create) an OpenRouter account and API key

#### Security note (chat)

***CAUTION*** The web UI has **no authentication** of its own. Anyone who can reach port 5052 can
open the Chat tab, read every saved conversation, and spend your OpenRouter credit - the same
caution about exposing the web/MCP port outside your home network applies here, more so.

- Tool results - including log lines and configuration - are sent to OpenRouter, and on to
  whichever provider serves the model you selected. `get_apps` always redacts credential-like
  values before they leave; unlike MCP, chat cannot ask for the unmasked `masked: false` form.
- Conversations are stored on disk and expire after `chat_expiry_days` of inactivity. Deleting a
  conversation hides it immediately, but Predbat's storage layer has no delete operation -
  deletion is a flag, not a removal - so **its stored copy remains readable to anyone with
  filesystem access until it expires**.
- The agent can read its own installed source (`search_source`, `read_source`), but the allowlist
  is by file extension, not by directory: `apps.yaml`, `secrets.yaml`, `predbat.log` and cached
  OAuth tokens can all sit in the same directory as the source being searched. `get_apps` and
  `get_log` are the only routes to that content, and both redact and filter what they return.
- `chat_web_search` costs roughly $0.001-0.015 per request. It is an OpenRouter-only plugin - if
  the active provider is not OpenRouter it is not sent at all, and a warning is logged
  once.
- `fetch_url` can only reach a small allowlist of hosts (the Predbat docs site and GitHub). This
  is deliberate, not a limitation to work around.
- `search_entities`, `get_entity_state` and `get_entity_history` can read **any** Home Assistant
  entity, not just Predbat's own - see [Reading Home Assistant state](#reading-home-assistant-state-mcp)
  above. They sit behind `switch.predbat_ai_ha_state_enable`, which is on by default. That is a
  materially larger disclosure than everything else on this page, so if you would rather the model
  saw only Predbat's own data, this is the switch to turn off.

#### Configuration Options (chat)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| Setting | Type | Default | Description |
| ------- | ---- | ------- | ----------- |
| `providers` | Dict | - | Named endpoints, each `{url, api_key, type, model}` - see [apps.yaml](apps-yaml.md#ai-chat-agent). Configuring one is what enables chat; there is no separate `chat_enable` setting. `model` sits on the provider rather than here, since a model id only means anything to the endpoint serving it |
| `max_tokens` | Integer | 0 | Maximum tokens per completion; `0` leaves it to the model/provider's own default |
| `max_tool_rounds` | Integer | 32 | Maximum model round trips (completions) within one turn before Predbat stops and asks you to continue. Every tool call the model makes inside one round trip still runs - this bounds round trips, not tool calls |
| `max_history` | Integer | 0 | Maximum recent messages sent to the model each turn, trimmed at a user-message boundary so a tool call and its reply are never split apart. Bounds cost, not how much is stored. `0` means unlimited |
| `max_conversations` | Integer | 20 | Conversations kept before the least recently updated are pruned |
| `expiry_days` | Integer | 30 | Days of inactivity before a conversation's stored copy expires |
| `turn_timeout` | Integer | 1800 | Seconds a whole turn may run, across every round trip |
| `request_timeout` | Integer | 300 | Seconds one completion request may take |
| `fetch_allowlist` | String list | docs site, `github.com`, `raw.githubusercontent.com` | Hosts `fetch_url` may reach |

All of these live inside the single `chat:` block in `apps.yaml`, which is the component's only
argument - the defaults above come from `CHAT_DEFAULTS` in `chat.py` rather than the component
registry, so each sits beside the code that reads it.

Three switches also control the chat agent. All three appear both under
[Config](web-interface.md#config-view) and in the Chat tab's own footer, so you can change your
mind about a permission without leaving the conversation:

| Entity | Default | Description |
| ------ | ------- | ----------- |
| `switch.predbat_chat_confirm_writes` | On | Hold every `set_config`, `set_plan_override` and `set_apps_config` call for your Approve/Reject before it runs |
| `switch.predbat_chat_web_search` | Off | Let the model search the wider web through OpenRouter's plugin - costs money per request, see above. The only one of the three that costs anything, and the only one off by default. Predbat's own `search_docs` does not go through it |
| `switch.predbat_ai_ha_state_enable` | On | Let `search_entities`, `get_entity_state` and `get_entity_history` read Home Assistant state - see [Reading Home Assistant state](#reading-home-assistant-state-mcp) above. Unlike the other two, this is `ai_`-prefixed rather than `chat_`-prefixed: it also gates the MCP server, not just this tab |

#### Available tools (chat)

Thirteen tools are shared with the [MCP server](#mcp-server-mcp) - see
[its tool table](#available-commands-mcp) above for what each one returns or does:

`get_status`, `get_plan`, `get_config`, `get_apps`, `get_apps_config`, `get_log`, `get_state`,
`get_entities`, `search_entities`, `get_entity_state`, `get_entity_history`, `set_config`,
`set_plan_override`

`search_entities`, `get_entity_state` and `get_entity_history` are the three that read arbitrary
Home Assistant state rather than just Predbat's own, and are off by default behind
`switch.predbat_ai_ha_state_enable` - see the Security note above and
[Reading Home Assistant state](#reading-home-assistant-state-mcp) in the MCP section. The Chat
tab's footer carries a live toggle for this switch, next to the model picker.

`set_config` and `set_plan_override` are two of the tools that write. With
`switch.predbat_chat_confirm_writes` on (the default), each one pauses in the transcript for your
Approve or Reject before it runs, showing the tool name and the exact arguments the model wants to
call it with. `set_apps_config`, below, is the third - and the only one of the three that is
chat-only rather than shared with MCP.

Six more tools only exist in chat:

| Tool | What it returns or does |
| ---- | ------------------------ |
| `set_chat_title` | Sets the conversation's title - the agent calls this itself, early in a new conversation |
| `search_docs` | Searches the published Predbat documentation, returning matching **sections** with an excerpt, a section id and its length (up to 10 results). Internal design documents under `superpowers/` are excluded |
| `read_docs` | Reads one documentation section in full by its section id, paged at 8,000 characters. Served from the cached search index, so it needs no network call - and returns just that section rather than a whole page of navigation and unrelated content |
| `search_source` | Searches Predbat's own installed source code - the exact version that is running - with a Python regular expression. Covers `.py`, `.cpp`, `.h`, `.hpp`, `.proto`, `.sh` and `.md` files only (up to 100 matches, five-second scan budget) |
| `read_source` | Reads a numbered slice of one source file found by `search_source` (up to 400 lines at a time) |
| `fetch_url` | Fetches a web page as text, restricted to the hosts in `fetch_allowlist` |
| `set_apps_config` | Changes one `apps.yaml` key - see below |

#### Changing apps.yaml from chat (chat)

`set_apps_config` changes a single `apps.yaml` setting - the same file the
[web apps.yaml editor](web-interface.md#apps-view) edits, through the same mechanism: it loads
the file with `ruamel.yaml`, so your comments and formatting survive, and writes it back with only
the one key changed.

It is deliberately narrow:

- **It can only change a key that already exists.** This is not a way to add new configuration -
  ask a key that is not already in your `apps.yaml` and the tool refuses, the same way the web
  editor does.
- **It refuses any credential-like key outright** - anything matching the same `_key`, `password`,
  `secret` or `token` heuristic `get_apps` masks. Neither the model nor an instruction hidden in
  something it read (a fetched web page, a documentation search result) can use this tool to set
  or swap an API key. The check runs on every part of a nested path, so
  `forecast_solar[0].api_key` is refused just as `ha_key` is.
- **It refuses the keys that decide where your credentials are sent** - `ha_url`,
  `openrouter_base_url` and anything ending `_url`, `_host` or `_endpoint`. Guarding the
  credential alone is not enough: repointing `ha_url` leaves `ha_key` untouched while sending it
  to somewhere else entirely.
- **It can change one value inside a nested structure**, using a path such as
  `forecast_solar[0].azimuth` - the same dotted-path syntax the web editor uses. This is how you
  change one roof's direction without rewriting the whole `forecast_solar` list. It matters for
  more than convenience: credentials are masked when the model reads them, so if it wrote a whole
  list back it would carry that mask into the file and overwrite the real key. Writing a
  container whose credential has been replaced by the mask is refused for exactly that reason,
  with an error pointing at the nested path instead.
- **It checks the new value's type against `apps.yaml`'s schema** where the key has one, so a
  value that would stop Predbat parsing its own configuration next start-up is refused before it
  is written, not discovered after a restart.
- **It backs up your `apps.yaml` to `apps.yaml.backup` first**, every time - the same safety net
  the raw whole-file editor gives you, extended to this tool too.

Because it is one of the three tools that write, `set_apps_config` is held for your Approve or
Reject when `switch.predbat_chat_confirm_writes` is on (the default, see above) - and unlike the
other two, the confirmation card shows the key's **current** value alongside the **proposed** one,
so you can see the actual change rather than just the model's intent. It also warns you there,
before you approve, that **saving restarts Predbat to apply the change**: the restart drops the
chat's live connection and ends that turn abruptly, but your conversation is not lost - it is
saved to Predbat's storage and is still there once Predbat has reconnected.

---

### GivEnergy Cloud Direct (gecloud)

**Can be restarted:** Yes

#### What it does (gecloud)

Connects directly to the GivEnergy Cloud to control your GivEnergy inverter and battery. This allows Predbat to automatically set charge/discharge times, power limits, and read real-time data from your inverter without relying on Home Assistant integrations.

#### When to enable (gecloud)

- You have a GivEnergy inverter
- You want direct cloud-based control (more reliable than local control)
- You have your GivEnergy Cloud API key
- You want automatic control of your battery

#### Important notes (gecloud)

- Requires a GivEnergy Cloud account and API key
- Can also control GivEnergy EV chargers and smart devices

#### Configuration Options (gecloud)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `ge_cloud_direct` | Boolean | Yes | - | `ge_cloud_direct` | Set to `true` to enable GivEnergy Cloud control |
| `api_key` | String | Yes | - | `ge_cloud_key` | Your GivEnergy Cloud API key |
| `automatic` | Boolean | No | false | `ge_cloud_automatic` | Set to `true` to automatically configured Predbat to use GivEnergy Cloud direct (no additional apps.yaml changes required) |
| `automatic_evc` | Boolean | No | false | `ge_cloud_automatic_evc` | Set to `true` to wire your GivEnergy EV chargers into `car_charging_energy`, `car_charging_planned` and `num_cars` — see [EV chargers](#ev-chargers-gecloud). Separate from `ge_cloud_automatic` because it registers a car |
| `evc_control` | Boolean | No | false | `ge_cloud_evc_control` | Set to `true` to let Predbat start and stop your EV charger from its car charging plan — see [Charger control](#charger-control-gecloud). Needs `ge_cloud_automatic_evc` |
| `load_today_ignore` | Boolean | No | false | `ge_cloud_load_today_ignore` | Set to `true` to ignore GE Cloud load_today data and use the `load_today` sensor from `apps.yaml` instead |
| `automatic_shared_ct` | Boolean | No | false | `ge_cloud_automatic_shared_ct` | Set to `true` to force shared CT clamp mode — only the first inverter's grid and load readings are used, preventing double-counting on multi-inverter systems with a single shared CT |
| `automatic_split_ct` | Boolean | No | false | `ge_cloud_automatic_split_ct` | Set to `true` to force split CT clamp mode — each inverter's readings are summed independently. Takes priority over `ge_cloud_automatic_shared_ct` if both are set |
| `automatic_split_pv` | Boolean | No | false | `ge_cloud_automatic_split_pv` | Set to `true` to also include standalone PV-only inverters' solar readings in `pv_today`/`pv_power`, in addition to battery inverters |

#### EV chargers (gecloud)

Every GivEnergy EV charger on the account is polled alongside the inverters and publishes
its meter readings as `sensor.predbat_gecloud_<serial>_evc_*` entities, plus two entities
describing the charger itself:

| Entity | Description |
| ------ | ----------- |
| `sensor.predbat_gecloud_<serial>_evc_status` | The charger's status as GivEnergy reports it, e.g. `charging`, `idle`, `offline` |
| `binary_sensor.predbat_gecloud_<serial>_evc_car_connected` | `on` while a car is plugged in, from the status above |

Those two entities are published whatever your settings say — they are new entities and
change nothing that already exists.

Setting `ge_cloud_automatic_evc` to `true` additionally wires the chargers into Predbat's
car planning, in serial order so charger N is car N:

- **car_charging_energy** — each charger's `_evc_energy_active_import_register`, so
  `car_charging_hold` subtracts the car charging from house load precisely instead of
  falling back to the `car_charging_threshold` heuristic
- **car_charging_planned** — each charger's `_evc_car_connected`, so Predbat only plans
  car charging when there is actually a car on the cable
- **num_cars** — raised to the number of chargers if it is currently lower, never reduced,
  since another component may have registered cars of its own

This is deliberately a separate setting from `ge_cloud_automatic` rather than part of it:
it registers a car and moves `num_cars`, which would change the plan for existing users
who had only ever asked for their inverter to be configured. It runs whether or not a
GivEnergy battery is present, so a GivEnergy charger alongside another manufacturer's
battery is configured too. Everything else about the car —
**car_charging_battery_size**, **car_charging_limit** and **car_charging_soc** — still
comes from `apps.yaml` as usual.

`car_charging_planned` is wired to a binary sensor rather than to the status sensor on
purpose: it answers `on`, which the default **car_charging_planned_response** already
matches, so this works without you having to add GivEnergy's status words to that list.
If your charger reports a status Predbat does not recognise it is treated as no car
connected and logged once, so please report the value from the log so it can be added.

#### Charger control (gecloud)

With `ge_cloud_evc_control` set to `true`, Predbat drives each charger from its own car's
plan: `start-charge` inside a planned charging window, `stop-charge` outside one. Charger N
follows car N, in the same serial order the automatic configuration uses, so the two cannot
disagree about which charger is which car.

`ge_cloud_automatic_evc` must also be on, since it is that configuration which establishes
the charger to car mapping. Predbat says so in the log and leaves control off rather than
guessing if you enable control without it.

- A command is only sent when the wanted state actually changes, so a charger already
  charging inside a window is left alone rather than commanded every minute
- A charger with no car plugged in is never commanded — Predbat waits for
  `_evc_car_connected` to go `on`
- Nothing is commanded until Predbat has published a car plan, so a restart cannot stop a
  charge that is already running

A `switch.predbat_gecloud_evc_control` entity appears when control is enabled, on by
default, so you can hand the charger back without editing `apps.yaml`. Turning it off — or
putting Predbat into read only mode — **releases** rather than just going quiet: if Predbat
had stopped the charger it sends `start-charge` once on the way out, so a car is never left
stranded by a charger Predbat walked away from. The switch state is saved, so an off
survives a restart.

Unlike a Zappi, there is no previous mode to restore on release: `start-charge` and
`stop-charge` are commands rather than modes, so your charger's own mode (Grid, Hybrid,
Solar) still decides what happens once Predbat lets go.

#### How to get your API key (gecloud)

1. Log in to your GivEnergy account at <https://www.givenergy.cloud>
2. Go to Settings → API Keys
3. Generate a new API key
4. Copy the key into your `apps.yaml` configuration

---

### GivEnergy Cloud Data (gecloud_data)

**Can be restarted:** Yes

#### What it does (gecloud_data)

Downloads historical energy data from GivEnergy Cloud including consumption, generation, battery usage, and grid import/export. This provides accurate historical data for Predbat's calculations and predictions.

#### When to enable (gecloud_data)

- You have a GivEnergy system
- You want Predbat to use historical data from GivEnergy Cloud instead of from Home Assistant.

#### Configuration Options (gecloud_data)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `ge_cloud_data` | Boolean | Yes | - | `ge_cloud_data` | Set to `true` to enable historical data download |
| `ge_cloud_key` | String | Yes | - | `ge_cloud_key` | Your GivEnergy Cloud API key (same as Cloud Direct) |
| `ge_cloud_serial` | String | No | Auto-detected | `ge_cloud_serial` | Your inverter serial number (usually auto-detected) |
| `days_previous` | List | No | [7] | `days_previous` | List of days to download data for, e.g., `[7]` for last week |

---

### Octopus Energy Direct (octopus)

**Can be restarted:** Yes

#### What it does (octopus)

Connects to your Octopus Energy account to automatically download your tariff rates, including support for dynamic tariffs like Agile and Intelligent Octopus. This ensures Predbat always has the most accurate and up-to-date energy pricing.

#### When to enable (octopus)

- You're an Octopus Energy customer
- You want automatic tariff updates
- You're on a variable tariff (Agile, Intelligent Octopus, etc.)
- You want to see your actual consumption data

#### Important notes (octopus)

- Works with all Octopus tariffs including Agile and Intelligent Octopus
- Automatically manages Intelligent Octopus smart charging slots
- Updates rates automatically, no manual intervention needed

#### Configuration Options (octopus)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `key` | String | Yes | - | `octopus_api_key` | Your Octopus Energy API key |
| `account_id` | String | Yes | - | `octopus_api_account` | Your Octopus Energy account number (starts with A-) |
| `automatic` | Boolean | No | true | `octopus_automatic` | Set to `true` to automatically configure Predbat to use this Component (no need to update apps.yaml) |

#### How to get your API credentials (octopus)

1. Log in to your Octopus Energy account at <https://octopus.energy>
2. Go to your account dashboard
3. Find your API key (usually in Developer settings)
4. Your account number is shown on your dashboard (format: A-XXXXXXXX)

---

### Axle Energy VPP (axle)

**Can be restarted:** Yes

#### What it does (axle)

Connects to Axle Energy's Virtual Power Plant (VPP) [UK] service to receive and track demand response events.
When Axle schedules export events, this component will track them and store the history for up to 7 days.
The component publishes a binary sensor that turns on when an event is currently active.

If configured in Predbat's `apps.yaml` Predbat will control your inverter to export in response to the Axle event and adjusts the export energy rate to account for the extra payment from Axle.

Sign up with my referral code here: <https://vpp.axle.energy/landing/grid?ref=R-VWIICRSA>

*Please note you are not allowed to be on Octoplus at the same time, so contact Octopus if you need to be removed from this scheme.*

Select control my battery for 'Events Only'.

#### When to enable (axle)

- You're enrolled in Axle Energy's VPP program and want Predbat to be aware of the scheduled events and plan for them.

#### Important notes (axle)

- Polls the Axle API every 10 minutes for updates
- Stores event history for up to 7 days
- Events are added to history as soon as they start (become active)
- Binary sensor (default name `binary_sensor.predbat_axle_event`) is `on` when an event is currently active, `off` otherwise
- Current event details and event history are available as sensor attributes ('event_current' and 'event_history')
- Alert notification sent when Predbat adds a new Axle VPP event to the Predbat plan
- When **axle_control** is enabled (set to `true` in `apps.yaml`), Predbat will enter read-only mode during active VPP events (default is `false`)
    - Read-only mode prevents Predbat from controlling the inverter while VPP events are running
    - Status will show as "Read-Only (Axle)" when this feature is active

#### Configuration Options (axle)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `api_key` | String | Yes | - | `axle_api_key` | Your Axle Energy API key from the VPP portal |
| `pence_per_kwh` | Integer | No | 100 | `axle_pence_per_kwh` | Payment rate in pence per kWh for VPP events |
| `automatic` | Bool | No | true | `axle_automatic` | When enabled use the default Axle event entity name (**binary_sensor.predbat_axle_event**) |
| `control` | Bool | No | false | `axle_control` | When enabled puts Predbat into Read-Only mode during Axle events |

#### How to get your API credentials (axle)

1. Log in to your Axle Energy VPP portal at <https://vpp.axle.energy>
2. Navigate to the Home Assistant integration section
3. Copy your API key
4. Paste it into `axle_api_key` in apps.yaml

#### Sensor Attributes (axle)

The binary sensor `binary_sensor.predbat_axle_event` provides the following attributes:

- `event_current`: List containing the current event (if any), with fields:
    - `start_time`: Event start time (timezone-aware datetime)
    - `end_time`: Event end time (timezone-aware datetime)
    - `import_export`: Event type ("import" or "export")
    - `updated_at`: Last update timestamp
    - `pence_per_kwh`: Payment rate for this event
- `event_history`: List of past events (up to 7 days) with the same fields as above

---

### SolaX Cloud API (solax)

**Can be restarted:** Yes

#### What it does (solax)

Connects directly to the SolaX Cloud API to control SolaX inverters and batteries.
This allows Predbat to automatically set charge/discharge schedules, power limits, target SOC, and read real-time data from your inverter without requiring local Home Assistant integrations.

The component polls your SolaX Cloud account every minute for real-time data and every 30 minutes for device and plant information.
It publishes comprehensive sensors for battery status, energy totals, and provides full control over charging and discharging schedules.

#### When to enable (solax)

- You have a SolaX inverter (X1, X3, X3-Hybrid, or other cloud-connected models)
- You want cloud-based control without local integrations
- You have SolaX Cloud API credentials (client ID and secret)
- You want automatic battery charge/discharge optimisation
- You want Predbat to read historical energy data directly from SolaX Cloud

#### Important notes (solax)

- Requires valid SolaX Cloud API credentials (client ID and client secret)
- Supports multiple plants/inverters with automatic discovery when `solax_automatic: true`
- Region-specific API endpoints: EU, US, or CN
- Authentication tokens are automatically managed (30-day expiry with auto-refresh)
- Supports both residential and commercial installations
- Control commands use time-window based scheduling (similar to GivEnergy)
- Read-only mode available with `solax_enable_controls: false`
- Compatible with Predbat's standard optimisation algorithms

#### Configuration Options (solax)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `client_id` | String | Yes | - | `solax_client_id` | Your SolaX Cloud API client ID |
| `client_secret` | String | Yes | - | `solax_client_secret` | Your SolaX Cloud API client secret |
| `region` | String | No | 'eu' | `solax_region` | API region: 'eu', 'us', or 'cn' |
| `plant_id` | String | No | None | `solax_plant_id` | Optional: Filter to specific plant ID |
| `automatic` | Boolean | No | false | `solax_automatic` | Auto-configure all entities and inverters |
| `enable_controls` | Boolean | No | true | `solax_enable_controls` | Enable inverter control (false for read-only) |

**Security Note:** Store `solax_client_id` and especially `solax_client_secret` in `secrets.yaml`:

```yaml
solax_client_id: !secret solax_client_id
solax_client_secret: !secret solax_client_secret
```

#### How to get your API credentials (solax)

1. Log in to your SolaX Cloud account:
   - EU: <https://www.solaxcloud.com>
   - US: <https://www.solaxcloud.us>
   - CN: <https://www.solaxcloud.com.cn>
2. Navigate to Settings → API Management (or Developer Settings)
3. Create a new API application or view existing credentials
4. Copy your **Client ID** and **Client Secret**
5. Add to your `secrets.yaml` file
6. Reference in `apps.yaml` using `!secret` notation

#### Published Entities (solax)

For each plant (replace `{plant_id}` with your actual plant ID), the component creates:

**Battery Sensors:**

- Battery SOC (kWh)
- Battery capacity (kWh)
- Battery temperature (°C)
- Battery max power (W)

**System Sensors:**

- Inverter max power (W)
- PV capacity (kWp)
- Total PV yield (kWh)
- Total battery charged (kWh)
- Total battery discharged (kWh)
- Total grid imported (kWh)
- Total grid exported (kWh)
- Total load (kWh, calculated)
- Total earnings (currency)

**Power Sensors (per inverter):**

- Charge/discharge power (W)
- Grid power (W)
- PV power (W)
- AC load power (W)

**Control Entities:**

- Battery reserve SOC (number, %)
- Charge start/end times (select, HH:MM:SS format)
- Charge target SOC (number, %)
- Charge rate (number, W)
- Charge enable (switch)
- Export start/end times (select, HH:MM:SS format)
- Export target SOC (number, %)
- Export rate (number, W)
- Export enable (switch)

#### Testing your configuration (solax)

You can test your SolaX Cloud API connection independently:

```bash
cd /config/appdaemon/apps/predbat
python3 solax.py --client-id YOUR_CLIENT_ID --client-secret YOUR_CLIENT_SECRET --region eu
```

This will authenticate, fetch all plants, devices, and real-time data, helping diagnose any connection or configuration issues.

---

### Ohme Charger (ohme)

**Can be restarted:** Yes

#### What it does (ohme)

Integrates with Ohme EV chargers to monitor charging sessions and coordinate charging with your energy tariff. Works particularly well with Intelligent Octopus to optimise charging times and costs.

#### When to enable (ohme)

- You have an Ohme EV charger
- You want Predbat to factor in the charging plan within Ohme, this is mostly used with Octopus Intelligent GO.

#### Important notes (ohme)

- Requires your Ohme account credentials
- Can automatically manage Intelligent Octopus charging slots
- Monitors real-time charging status and energy consumption
- Publishes `sensor.predbat_ohme_energy_today`, the energy delivered to the car today, and wires it to `car_charging_energy` when `ohme_automatic` is set.
  See [Ohme charge energy](car-charging.md#ohme-charge-energy)

#### Configuration Options (ohme)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `email` | String | Yes | - | `ohme_login` | Your Ohme account email address |
| `password` | String | Yes | - | `ohme_password` | Your Ohme account password |
| `ohme_automatic` | Boolean | No | `False` | `ohme_automatic` | Set to `true` to register the Ohme charger with Predbat as a car |
| `ohme_control` | Boolean | No | `False` | `ohme_control` | Set to `true` to let Predbat start and stop the charger from its own plan. Requires `ohme_automatic`; released by read only mode |
| `ohme_automatic_octopus_intelligent` | Boolean | No | unset (auto-detect) | `ohme_automatic_octopus_intelligent` | Take the Intelligent car slots from Ohme. Omit the setting entirely to auto-detect it when `ohme_automatic` is on, or give it `true`/`false` to override. Do not write `auto` - any value other than `true`/`false` is read as true |

---

### myenergi (myenergi)

**Can be restarted:** Yes

#### What it does (myenergi)

Monitors myenergi Zappi EV chargers and Eddi hot water diverters, publishing their status, power and session energy as Predbat entities, and provides send-boost and cancel-boost controls.

Predbat supports both of myenergi's APIs:

- **Direct** (default) — HTTP digest authentication against `director.myenergi.net`, using your hub serial number and an API key you generate yourself. This is the same API the `ha-myenergi` Home Assistant integration uses, and any myenergi owner can set it up today.
- **Cloud OAuth** — the official 3rd party API at `api.s18.myenergi.net`. This needs credentials issued by myenergi through their partner registration process.

#### When to enable (myenergi)

- You have a Zappi or an Eddi and want Predbat to account for their energy use when planning
- You want Predbat to publish sensors for their status, power and session energy
- You want to trigger or cancel a boost from Home Assistant

#### Important notes (myenergi)

- With `myenergi_automatic` on (the default), Predbat sets three `apps.yaml` values for you:
    - `car_charging_energy` — every Zappi's session energy, so charging is subtracted from your house load rather than being learnt as base load. Ensure `switch.predbat_car_charging_hold` is on (it is by default) for that subtraction to take effect
    - `car_charging_planned` — every Zappi's plug status sensor, one entry per car, so Predbat knows when the car is plugged in and due to charge. The regex the `apps.yaml` templates ship for this key matches the third-party `ha-myenergi` integration's entity names, not the ones Predbat publishes, so without this Predbat would fall back to the `car_charging_threshold` heuristic
    - `iboost_energy_today` — the first Eddi's session energy (first by serial number). This feeds the iboost model, and it is also subtracted from your historical house load whenever `switch.predbat_iboost_energy_subtract` is on (the default), which happens whether or not iboost itself is enabled
- Auto-configuration runs once, after the first poll that returns devices. A Zappi or Eddi added later is published as entities but is not wired into those keys until Predbat restarts
- If you set `car_charging_planned` yourself in `apps.yaml`, Predbat logs a note and auto-discovery still wins — remove your entry to silence it
- Predbat's shipped `car_charging_planned_response` list covers the plug states a Zappi reports when the car is connected, including `ev ready to charge`. If you maintain your own list, add that value or Predbat will treat a car that is plugged in and waiting as not planned to charge
- Boosting a Zappi is only accepted by myenergi while it is in Eco or Eco+ mode
- Set `myenergi_enable_controls` to `false` for monitor-only operation — the boost switches are still published but stop responding

#### Configuration Options (myenergi)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `auth_method` | String | No | `direct` | `myenergi_auth_method` | `direct` (local digest API) or `oauth` (official cloud API) |
| `hub_serial` | String | No | - | `myenergi_hub_serial` | Hub serial number — required when `auth_method` is `direct`. It is the API login rather than a device filter, so it cannot default to all devices; one serial reads every device on the account. With no hub, use the serial the API key was generated against |
| `api_key` | String | No | - | `myenergi_api_key` | API key generated at myaccount.myenergi.com — required when `auth_method` is `direct` |
| `key` | String | No | - | `myenergi_key` | OAuth access token, cloud transport |
| `token_hash` | String | No | - | `myenergi_token_hash` | OAuth refresh token hash, used to refresh `key` automatically. At least one of `key` or `token_hash` is required when `auth_method` is `oauth` |
| `token_expires_at` | String | No | - | `myenergi_token_expires_at` | OAuth access token expiry, used to trigger a refresh |
| `automatic` | Boolean | No | true | `myenergi_automatic` | Set to `false` to stop Predbat wiring the device sensors into `car_charging_energy`, `car_charging_planned` and `iboost_energy_today` automatically |
| `enable_controls` | Boolean | No | true | `myenergi_enable_controls` | Set to `false` for monitor-only operation |
| `poll_seconds` | Integer | No | 60 | `myenergi_poll_seconds` | Poll interval in seconds, rounded to the nearest whole multiple of 60, minimum 60 and maximum 1800 (a longer gap would make Predbat's own health check report the component as failed) |
| `zappi_control` | Boolean | No | false | `myenergi_zappi_control` | Set to `true` to let Predbat drive your Zappi from its car charging plan — see [Zappi charge control](#zappi-charge-control-myenergi) |

The component only starts when at least one of `myenergi_api_key`, `myenergi_key` or `myenergi_token_hash`
is set. That test is a plain any-of and does not look at `myenergi_auth_method`, so a credential belonging
to the transport you did not select still starts the component — it then logs which setting is missing
rather than failing silently.

Example for the direct transport:

```yaml
myenergi_hub_serial: '12345678'
myenergi_api_key: !secret myenergi_api_key
```

#### How to get your API key (myenergi)

1. Sign in at <https://myaccount.myenergi.com>.
2. Open **Advanced** then **API Key**.
3. Generate a key for your hub and copy it.
4. Your hub serial number is printed on the hub and shown in the myenergi app. If you have no hub, use the serial of the device
   acting as one — the Zappi or Eddi you generated the key against. Either way a single serial is enough, because it is the
   login for the account rather than a choice of which device to read: Predbat discovers every Zappi and Eddi behind it.

#### Published entities (myenergi)

Per Zappi (`{sn}` is the device serial number):

- `sensor.predbat_myenergi_zappi_{sn}_status`, `_mode`, `_plug_status`, `_power`, `_session_energy`
- `binary_sensor.predbat_myenergi_zappi_{sn}_charging`
- `switch.predbat_myenergi_zappi_{sn}_boost`, `number.predbat_myenergi_zappi_{sn}_boost_energy`

Per Eddi:

- `sensor.predbat_myenergi_eddi_{sn}_status`, `_power`, `_session_energy`, `_temp_1`, `_temp_2`
- `switch.predbat_myenergi_eddi_{sn}_boost`, `number.predbat_myenergi_eddi_{sn}_boost_minutes`

The Eddi temperature sensors are only published when a probe is connected.

#### Controls (myenergi)

Turning a boost switch on sends a boost of the amount selected on the companion number entity — kWh for a Zappi, minutes for an Eddi. Turning it off cancels the boost. The switch state is read back from the device, so a boost started or stopped in the myenergi app is reflected here too.

myenergi only accepts a Zappi boost while the charger is in Eco or Eco+ mode. Predbat checks that one condition before calling, and logs a warning instead. Every other reason a boost can be refused — an Eddi already at its maximum tank temperature, for instance — is only discovered from myenergi's reply, so Predbat issues the call and logs `myenergi: control failed` when it comes back refused. The switch reverts to the device's real state on the next poll either way.

Not implemented in this release: priority, minimum green level, phase setting, and charging schedules. These exist on the transport interface as reserved methods, ready for a later release, and nothing in Predbat calls them — there is no entity or service that can reach them, so there is nothing for you to try. If a future release wires one up before it is implemented, it warns once per control rather than failing silently. Super schedules, managed mode and Libbi batteries are out of scope entirely for this release; the component only supports Zappi and Eddi devices and does not expose any control surface for them.

#### Zappi charge control (myenergi)

With `myenergi_zappi_control: true` Predbat drives your Zappi from the car charging plan it has already worked out, instead of you scheduling the charge on the Zappi itself.

Inside a planned charging window Predbat puts the Zappi in **Fast**, and outside one it puts it in **Stopped**. Fast is used because the window was chosen for its electricity rate rather than for sunshine — Eco or Eco+ would only charge from surplus, and the car would not get what the plan assumed.

Each Zappi follows its own car. Zappis are matched to cars in serial number order, the same order `car_charging_energy` and `car_charging_planned` are wired in, so your first Zappi follows car 0's plan, your second follows car 1's, and so on.

Predbat re-checks the Zappi every minute. If the mode is changed in the myenergi app while Predbat is in control, it is put back — otherwise control would drift away silently.

##### The control switch

A `switch.predbat_myenergi_zappi_control` entity appears once `myenergi_zappi_control` is set. It starts **on**, and turning it off hands your Zappi back without editing `apps.yaml`. The setting is remembered across restarts, so a restart will not quietly take control back.

##### When Predbat hands the Zappi back

Predbat releases the Zappi when the control switch is turned off, or when Predbat itself is put in read only mode. Releasing restores the mode the Zappi was in before Predbat first changed it, falling back to **Eco+** when there is nothing saved — after a restart, for instance. It deliberately does not simply stop sending commands, because Predbat may have left the Zappi Stopped and walking away would leave the car unable to charge.

##### Two things to expect

Charge control needs `myenergi_automatic`, because it is automatic configuration that establishes which Zappi belongs to which car. It also needs `myenergi_enable_controls`. If either is off, Predbat logs which one and leaves the Zappi alone.

While Predbat is in control the Zappi is in Fast or Stopped, and myenergi only accepts a boost in Eco or Eco+ — so the manual boost switch will refuse for as long as control is on. Turn the control switch off if you want to boost by hand.

Outside a planned window the Zappi is Stopped, which means it will not divert surplus solar to the car either. If you would rather keep solar diversion, leave `myenergi_zappi_control` off and let the Zappi run its own modes.

#### Known limitation (myenergi)

The session energy sensors reset to zero when a charging or heating session ends. Predbat expects that: it treats these sensors as incrementing counters and rebases the series whenever it sees one reset, so both the per-minute load subtraction and the daily `iboost_today` total come out right across any number of sessions in a day.

The one case it cannot see is a small session. A drop of less than 1 kWh is smoothed over as a dip in the data rather than treated as a reset, so a session that finishes below roughly 1 kWh — a short top-up, or a brief Eddi diversion — can be missed and its energy left out of the day's figures. A reading of zero between the sessions does not help, because the dip is smoothed away before the reset is looked for. That applies equally to `car_charging_energy` and to `iboost_today`. In practice it is a fraction of a kWh, and the planner mostly cares about the larger sessions, but the daily totals can read slightly low if your Zappi or Eddi does a lot of very short sessions.

#### Testing your configuration (myenergi)

You can test either transport independently of Predbat:

```bash
cd /config/appdaemon/apps/predbat
python3 myenergi.py --hub-serial YOUR_HUB_SERIAL --api-key YOUR_API_KEY
```

Add `--boost zappi` or `--boost eddi` (with `--amount`) to send a test boost, or `--cancel-boost zappi`/`--cancel-boost eddi` to cancel one. Use `--token` in place of `--hub-serial`/`--api-key` to test the cloud OAuth transport instead.

To try the charge control commands against a real Zappi without enabling the feature, `--start-charge` puts it in Fast exactly as a planned window does, `--stop-charge` puts it in Stopped as being outside one does, and `--release` puts it back in Eco+ as handing it back does. Run the command again with no action to see the mode that took effect.

Note `--stop-charge` leaves the Zappi stopped, so remember to `--release` it afterwards or set the mode you want in the myenergi app.

---

### Fox ESS API (fox)

**Can be restarted:** Yes

#### What it does (fox)

Integrates with Fox ESS inverters for monitoring and controlling Fox ESS battery systems. Similar to GivEnergy Cloud Direct, but for Fox ESS equipment.

#### When to enable (fox)

- You have a Fox ESS inverter
- You want direct API control of your Fox system
- You have your Fox ESS API key

#### Important notes (fox)

- Requires Fox ESS Cloud account and API key

#### Configuration Options (fox)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `key` | String | Yes | - | `fox_key` | Your Fox ESS API key |
| `automatic` | Boolean | No | false | `fox_automatic` | Set to `true` to automatically configured Predbat to use the Fox inverter (no manual apps.yaml updates required) |
| `automatic_ignore_pv` | Boolean | No | false | `fox_automatic_ignore_pv` | When `automatic` is enabled, set to `true` to prevent Fox Cloud from overwriting `pv_power` and `pv_today` config. Useful for AC-coupled setups where PV is measured independently and Fox Cloud reports zero/absent PV data |

---

### Tesla Powerwall Teslemetry API (teslemetry)

**Can be restarted:** Yes

!!! warning "Beta"
    This component is in **beta** and under active development. It is not yet recommended for general use - expect issues and please report them on GitHub. For a proven setup today, use the [manual Home Assistant integration](inverter-setup.md#manual-configuration-via-home-assistant-integrations) instead.

#### What it does (teslemetry)

Integrates a Tesla Powerwall via the [Teslemetry](https://teslemetry.com) REST API (which mirrors Tesla Fleet API paths, so a direct Fleet API connection works by changing the base URL). Publishes live power flows, SOC and daily energy sensors, and exposes fox-style charge/discharge window entities that Predbat programs directly. Because the Powerwall has no native scheduler, the component translates the programmed windows into operation mode, backup reserve, grid-charging and export-rule commands each cycle, including the export tariff-trick needed to force the Powerwall to export.

#### When to enable (teslemetry)

- You have a Tesla Powerwall (developed against Powerwall 3)
- You want Predbat to control charging and export directly via the Tesla cloud
- You have a Teslemetry subscription and API token (or Tesla Fleet API access)

#### Important notes (teslemetry)

- Export freeze is not supported by the Powerwall hardware and is disabled automatically
- The Powerwall has no charge/discharge rate control; rates are modelled from the nameplate power
- When enabled (and Predbat is not read-only) the component owns the device tariff, publishing Predbat's real import/export rates (quantised into a few time-of-use bands) so they show correctly in the Tesla app, with a synthetic high-price `ON_PEAK` band over the committed discharge window to drive export
- Export start/stop is driven each cycle by the operation-mode and export-rule commands; the tariff is pushed only when the rates or the discharge window actually change, to conserve Teslemetry's monthly API-call budget
- The four diagnostic control entities (operation mode, backup reserve, grid charging, allow export) mirror the emulator's asserted state; any manual change made to them is re-asserted away within about a minute while Predbat is not read-only

#### Configuration Options (teslemetry)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `key` | String | Yes | - | `teslemetry_key` | Bearer token. In `api_key` mode this is your static Teslemetry token; in `oauth` mode it is the Tesla Fleet API access token (refreshed automatically) |
| `site_id` | String or String List | No | First account site | `teslemetry_site_id` | Optional Tesla energy site id (or list of ids) to filter the sites discovered from the account; leave unset to use the first site on the account automatically |
| `base_url` | String | No | `https://api.teslemetry.com` | `teslemetry_base_url` | REST base URL; for direct Fleet API set this to your regional Fleet endpoint (e.g. `https://fleet-api.prd.eu.vn.cloud.tesla.com`) |
| `automatic` | Boolean | No | false | `teslemetry_automatic` | Set to `true` to automatically configure Predbat to use the Powerwall (no manual apps.yaml inverter settings required) |
| `auth_method` | String | No | `api_key` | `teslemetry_auth_method` | `api_key` (static Teslemetry token) or `oauth` (direct Tesla Fleet API). In `oauth` mode the OAuth flow and token refresh are handled for you by predbat.com - the same way the Fox integration works - so `oauth` requires connecting via predbat.com; self-hosted users use `api_key` |

---

### Enphase API (enphase)

**Can be restarted:** Yes

#### What it does (enphase)

Connects Predbat to the Enphase Enlighten cloud for monitoring and battery control of Enphase IQ Battery systems, with no local hardware access required. Predbat logs in through the same web endpoints used by the Enlighten app/web site, publishes monitoring sensors, and can write battery schedules to control charging and discharging.

#### When to enable (enphase)

- You have an Enphase IQ Battery system managed through the Enlighten app
- You want cloud-based monitoring and control without any local integration
- Your Enphase account does not have multi-factor authentication (MFA) enabled

#### Important notes (enphase)

- **EXPERIMENTAL**: this uses the unofficial Enlighten web-app API - there is no official Enphase API with battery control, and Enphase may change it without notice
- Accounts with multi-factor authentication (MFA) enabled are **not supported** - disable MFA on the Enphase account before use
- Predbat controls the battery by writing Enphase schedules: charge windows become charge-from-grid (CFG) schedules with a target SOC, export windows become discharge-to-grid (DTG) schedules, freeze-export windows use restrict-battery-discharge (RBD) schedules, and the reserve is set through the battery profile. `automatic_config` requires both CFG and DTG support and fails configuration if either is missing
- On a successful write, Predbat optimistically updates its local cache and moves on rather than waiting to re-read the cloud - the periodic schedule/profile re-read (every 30 minutes) corrects the cache later if a write didn't actually land
- The PV, grid, battery and load power sensors come from the Enlighten livestream: once per cycle Predbat connects to Enphase's AWS IoT broker over MQTT, takes one measured reading and disconnects. These are separately metered channels, so they are instantaneous and the house load is a real measurement rather than a calculation
- A livestream reading stays in use for up to 15 minutes, so a single failed connection does not disturb the sensors. Past that they fall back rather than keep presenting an old measurement as current, and readings are never carried across a restart
- In that fallback, all four come from the same 15-minute energy bucket of the cloud's intra-day data, so they still agree with each other and a power-flow display still balances. The cloud keeps back-filling a bucket for several minutes after it closes, so Predbat reads a bucket that has settled - which means the fallback values lag real time by roughly 15 to 30 minutes
- In that fallback, load power is the energy-balance residual (PV + grid + battery), which is how the Enphase cloud derives its own consumption figure. Because it is a small difference between much larger numbers, it becomes unreliable while the battery is charging or discharging hard - it is clamped at zero so it can never show a negative house load, but treat it as indicative only during battery activity. The energy (`*_today`) sensors are unaffected either way and remain accurate
- **Predbat owns the battery schedules**: it drives exactly one window per direction, so unless it is in read-only mode it deletes any other CFG/DTG/RBD schedule it finds on the site, including ones you created in the Enlighten app. Do not add your own battery schedules while Predbat is in write mode - the Enphase cloud rejects any overlapping schedule with an HTTP 409 conflict, which would stop Predbat from controlling the battery. Set Predbat to read-only mode if you want to manage schedules yourself
- A window that is no longer needed is deleted rather than disabled, because the Enphase cloud ignores a request to disable a schedule (it reports success but keeps enforcing the window)
- Repeated login failures back off automatically to protect the Enphase account from lockout: a 5 minute cooldown after each rejection, rising to a 24 hour suspension after 3 consecutive rejections

#### Configuration Options (enphase)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `username` | String | Yes | - | `enphase_username` | Your Enphase Enlighten account e-mail address |
| `password` | String | Yes | - | `enphase_password` | Your Enphase Enlighten account password |
| `site_id` | String | No | First site found | `enphase_site_id` | Restrict Predbat to a single Enlighten site id |
| `automatic` | Boolean | No | false | `enphase_automatic` | Set to `true` to automatically configure Predbat to use the Enphase inverter (no manual apps.yaml sensor updates required) |
| `automatic_ignore_pv` | Boolean | No | false | `enphase_automatic_ignore_pv` | When `automatic` is enabled, set to `true` to prevent Enphase Cloud from overwriting `pv_power` and `pv_today` config |

#### Published Entities (enphase)

For each site (`{site_id}` in the entity names), the component creates:

**Battery Sensors:**

- Battery SOC (%)
- Battery available energy (kWh)
- Battery capacity (kWh)
- Battery max rate (W)
- Battery status
- Battery profile
- Battery reserve (%)
- Battery reserve minimum (%)

**Energy Sensors:**

- PV/load/import/export today (kWh)
- Battery charge/discharge today (kWh)

**Power Sensors (derived from the most recent completed 15-minute energy bucket):**

- PV power, grid power, battery power, load power (W)

**Control Entities (per site):**

- Battery schedule reserve (number, %) - written to Enphase immediately on change, like Fox
- Charge/export start and end time (select, HH:MM:SS format)
- Charge/export target SOC (number, %)
- Charge/export enable (switch)
- Charge/export write (switch) - triggers the cloud write for that schedule

A configured site always supports both charge and export control - `automatic_config` requires both
the charge-from-grid (CFG) and discharge-to-grid (DTG) schedule families to be available, so both sets
of controls are always published. There is no separate freeze control: freeze-export is derived
automatically (and written as a restrict-battery-discharge schedule) whenever the export target SOC is
set to exactly 99%; 100% already means export is disabled.

---

### Solis Cloud API (solis)

**Can be restarted:** Yes

#### What it does (solis)

Integrates with Solis inverters for monitoring and controlling Solis battery systems via the Solis Cloud API. Provides direct control of charge/discharge schedules, storage modes, and battery parameters.

#### When to enable (solis)

- You have a Solis hybrid inverter with battery storage
- You want direct API control of your Solis system
- You have your Solis Cloud API credentials

#### Important notes (solis)

- **EXPERIMENTAL**: This is a new integration and may have issues
- Requires Solis Cloud account with API access
- **Battery size configuration**: The Solis Cloud integration cannot automatically determine your battery size from the inverter. You can either:
    - Manually set `soc_max` in `apps.yaml` with your battery capacity in kWh (recommended), or
    - Leave `soc_max` unset and allow Predbat to automatically detect battery size from historical charging data (requires several days of data)
- Supports both V1 (older firmware) and V2 (newer firmware) time window formats
- Automatic configuration available - sets up all required Predbat sensors automatically
- **PV-only inverters**: an inverter Solis Cloud reports as having no battery is never managed as a battery inverter and is never written to, but its generation is still included in `pv_today` and `pv_power` so the array total covers the whole roof. Its load and grid readings are left out, as those registers can overlap with the battery inverter's on a shared-CT installation - set `pv_today`/`pv_power`/`load_today` manually with `solis_cloud_pv_load_ignore: true` if you need something different
- **Inverter timezone must match Predbat's `timezone` setting**: charge/discharge slot times are written to the inverter as plain `HH:MM` values with no timezone attached. The inverter interprets these using its own configured timezone, not Predbat's. If your inverter's timezone is set to UTC (or anything other than Predbat's `timezone`, `Europe/London` by default), the resulting charge/discharge windows will be offset by the difference - for example, a full hour out whenever British Summer Time is in effect. Set the inverter's own timezone to match Predbat's `timezone` setting to avoid this.

#### Configuration Options (solis)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `api_key` | String | Yes | - | `solis_api_key` | Your Solis Cloud API Key (KeyId) |
| `api_secret` | String | Yes | - | `solis_api_secret` | Your Solis Cloud API Secret (KeySecret) |
| `inverter_sn` | String/List | No | - | `solis_inverter_sn` | Inverter serial number(s) - Leave unset to see all. Single string or list of strings for multiple inverters |
| `automatic` | Boolean | No | false | `solis_automatic` | Set to `true` to automatically configure Predbat to use the Solis inverter (no manual apps.yaml sensor updates required) |
| `base_url` | String | No | Auto-detected | `solis_base_url` | Solis Cloud API base URL (automatically selects correct region) |
| `control_enable` | Boolean | No | true | `solis_control_enable` | Enable/disable control commands (set to false for monitoring only) |
| `nominal_voltage` | Float | No | - | `solis_nominal_voltage` | Your battery's nominal pack voltage (e.g. cell count x nominal cell voltage), used only for the battery capacity sensor. Not the same as the live measured battery voltage. Without it, the capacity sensor is still published but flagged unreliable - see [apps.yaml](apps-yaml.md#solis-cloud-api) |

---

### Sigenergy Cloud API (sigenergy)

**Can be restarted:** Yes

#### What it does (sigenergy)

Integrates with Sigenergy (SigenStor) inverter and battery systems via the Sigenergy OpenAPI (REST) and MQTT broker.
No local Home Assistant integration is required — Predbat connects directly to the Sigenergy cloud, publishes all needed sensor entities, and can automatically wire itself to use them.

Supports real-time monitoring (SOC, power flows, operational mode) and full charge/discharge control including reserve, charge target, and export target SoC.

#### When to enable (sigenergy)

- You have a Sigenergy (SigenStor) inverter with battery storage
- You want cloud-based control without a local modbus

#### Important notes (sigenergy)

- **EXPERIMENTAL**: This is a new integration and may have issues
- The Sigenergy Developer Portal application must have **VPP Mode** enabled or charge/discharge commands will be rejected
- On first startup Sigenergy sends an **onboarding approval email** — you must click the approval link before live MQTT data starts flowing
- MQTT certificates (CA, client cert, client key) are required for TLS-authenticated connections to the broker

#### Configuration Options (sigenergy)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `app_key` | String | Yes | - | `sigenergy_app_key` | Your Sigenergy Application Key from the Developer Portal |
| `app_secret` | String | Yes | - | `sigenergy_app_secret` | Your Sigenergy Application Secret |
| `ca_cert` | String | No | System CAs | `sigenergy_ca_pem` | PEM text of the CA certificate for TLS verification |
| `client_cert` | String | No | - | `sigenergy_client_pem` | PEM text of the client certificate for mutual TLS |
| `client_key` | String | No | - | `sigenergy_client_key` | PEM text of the client private key for mutual TLS |
| `system_id` | String/List | Yes | n/a | `sigenergy_system_id` | Must be set to onboard systems. Find your System ID in the SigEnergy app under **Settings → System Settings → About → System ID** (tap to copy) |
| `automatic` | Boolean | No | false | `sigenergy_automatic` | Set to `true` to automatically configure Predbat sensors and controls (recommended) |
| `enable_controls` | Boolean | No | true | `sigenergy_enable_controls` | Set to `false` for monitoring only — no charge/discharge commands will be sent |
| `base_url` | String | No | EU endpoint | `sigenergy_base_url` | Override the REST API base URL (e.g. for non-EU regions) |
| `mqtt_host` | String | No | Derived from base_url | `sigenergy_mqtt_host` | Override the MQTT broker hostname |

#### Configuration example (sigenergy)

In `apps.yaml`:

```yaml
  sigenergy_app_key: !secret sigenergy_app_key
  sigenergy_app_secret: !secret sigenergy_app_secret
  sigenergy_ca_pem: !secret sigenergy_ca_pem
  sigenergy_client_pem: !secret sigenergy_client_pem
  sigenergy_client_key: !secret sigenergy_client_key
  sigenergy_automatic: true
  sigenergy_system_id: MY_SYSTEM_ID
```

In `secrets.yaml` (certificates use YAML literal block scalars — every line of the PEM must be indented):

```yaml
sigenergy_app_key: "your-app-key-here"
sigenergy_app_secret: "your-app-secret-here"

sigenergy_ca_pem: |
  -----BEGIN CERTIFICATE-----
  ... note entire key must be indented 2 spaces
  -----END CERTIFICATE-----

sigenergy_client_pem: |
  -----BEGIN CERTIFICATE-----
  ... note entire key must be indented 2 spaces
  -----END CERTIFICATE-----

sigenergy_client_key: |
  -----BEGIN RSA PRIVATE KEY-----
  ... note entire key must be indented 2 spaces
  -----END RSA PRIVATE KEY-----
```

See [Sigenergy Cloud setup](inverter-setup.md#sigenergy-cloud) for the full credential-acquisition walkthrough.

---

### DEYE Cloud API (deye)

**Can be restarted:** Yes

#### What it does (deye)

Integrates with DEYE (Sunsynk-family) hybrid inverters via the DeyeCloud OpenAPI, providing direct cloud-based monitoring and battery control - no local Modbus/RS485 access is required. Predbat discovers every battery inverter registered against the account, publishes monitoring sensors, and writes Fox-style schedule control entities that are combined into a single `strategy_dynamic_control` command each cycle.

#### When to enable (deye)

- You have a DEYE (or Sunsynk-family) hybrid inverter with battery storage registered on DeyeCloud
- You want direct cloud-based monitoring and control without local hardware access
- You have created a DeyeCloud developer app (App ID/Secret), or you are using Predbat.com

#### Important notes (deye)

- **EXPERIMENTAL**: This is a new integration and may have issues
- Two deployment modes are supported: the self-hosted Home Assistant add-on manages its own DeyeCloud token from developer app credentials (`deye_auth_method: 'app_credentials'`, the default), while Predbat.com injects and refreshes the token for you (`deye_auth_method: 'oauth'`)
- DEYE is mode-less like Enphase/Tesla: Predbat only owns the charge window, export window, reserve and target SOCs - the component derives the internal DEYE work mode (`SELLING_FIRST` for export, `ZERO_EXPORT_TO_CT` for charge/hold/idle) automatically from that intent. `ZERO_EXPORT_TO_CT` measures at the grid CT, so the battery serves the whole house without exporting; the stricter `ZERO_EXPORT_TO_LOAD` measures at the inverter's own output and would stop the battery serving anything not wired to it
- Solar Sell is always left on. It governs whether surplus PV reaches the grid, not what the battery does, so turning it off outside export windows would curtail spare solar for most daylight hours - export windows are driven by the work mode and the slot SOC targets instead
- Predbat never enables charging from an external generator. The per-slot generator flag is the owner's setting, not Predbat's: it is read from the inverter and carried through unchanged when Predbat rewrites the programme, and left off when it cannot be read
- Self-use TOU slots are written at the inverter's rated power. Zero slot power is how DEYE expresses a freeze (the battery neither charges nor discharges), so it is used only for freeze-charge and freeze-export; if neither the inverter rating nor the battery config is known, Predbat skips the control write rather than send a slot power it cannot justify
- Freeze-charge is implemented via the reserve, and freeze-export is signalled by setting the export target SOC to 99% (100% already means export is disabled)
- Writes are combined into one atomic `strategy_dynamic_control` call per cycle with change detection (no write when the payload is unchanged), and the resulting `orderId` is polled asynchronously until success
- The reserve control entity writes to DeyeCloud immediately on change, like Fox and Enphase

#### Configuration Options (deye)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `app_id` | String | No | - | `deye_app_id` | Your DeyeCloud developer app's App ID (self-hosted add-on only) |
| `app_secret` | String | No | - | `deye_app_secret` | Your DeyeCloud developer app's App Secret (self-hosted add-on only) |
| `username` | String | No | - | `deye_username` | Your DeyeCloud account e-mail address or username (self-hosted add-on only) |
| `password` | String | No | - | `deye_password` | Your DeyeCloud account password (self-hosted add-on only) |
| `data_center` | String | No | `eu` | `deye_data_center` | DeyeCloud region: `eu`, `am` or `india` |
| `company_id` | String | No | - | `deye_company_id` | Optional, only needed for installer/business accounts |
| `auth_method` | String | No | `app_credentials` | `deye_auth_method` | `app_credentials` (self-managed token, HA add-on) or `oauth` (token injected and refreshed by Predbat.com) |
| `inverter_sn` | String/List | No | All found | `deye_inverter_sn` | Restrict Predbat to specific inverter serial number(s) - single string or list |
| `automatic` | Boolean | No | false | `deye_automatic` | Set to `true` to automatically configure Predbat to use the discovered DEYE inverter(s) (no manual apps.yaml sensor updates required) |
| `automatic_ignore_pv` | Boolean | No | false | `deye_automatic_ignore_pv` | When `automatic` is enabled, set to `true` to prevent DEYE Cloud from overwriting the `pv_power` config |

See [DEYE Cloud setup](inverter-setup.md#deye-cloud) for the full credential-acquisition walkthrough.

---

### Sunsynk Cloud API (sunsynk)

**Can be restarted:** Yes

#### What it does (sunsynk)

Integrates with Sunsynk (DEYE-family) hybrid inverters via the Sunsynk Connect cloud API, providing cloud-based monitoring and, once confirmed against your own hardware, battery control - no local Modbus/RS485 access is required. Predbat discovers every inverter registered against the account, publishes monitoring sensors and derived battery ratings, and writes DEYE-style schedule control entities that are combined into a single settings write each cycle.

#### When to enable (sunsynk)

- You have a Sunsynk hybrid inverter with battery storage registered on Sunsynk Connect (the same account used by the Sunsynk phone app)
- You want cloud-based monitoring, with optional battery control, and no local hardware or Modbus dongle access
- You have your Sunsynk Connect account e-mail and password, or you are using Predbat.com

#### Important notes (sunsynk)

- **EXPERIMENTAL:** nobody on the Predbat project has a Sunsynk account, so the wire format is inferred from third-party open-source clients rather than from documentation. Every request and response is traced to the log (`api_debug`, on by default) with credentials redacted, so a tester can capture evidence for an issue report
- **Control is on by default**, the same as `solis_control_enable`. Set `sunsynk_control_enable: false` for monitoring only - Predbat then reads telemetry and settings but never writes to the inverter. Because the write format is inferred rather than documented, it is worth running the [diagnostics CLI](inverter-setup.md#sunsynk-cloud) against your own inverter first to confirm the settings layout, and switching control off if anything looks wrong
- **Three login methods.** `sunsynk_auth_method` selects `password` (RSA-encrypted login, the default), `password_legacy` (the pre-2025 plaintext login, opt-in) or `oauth` (Predbat.com injects and refreshes the token). `password` never falls back to `password_legacy` automatically - choosing the plaintext login is a deliberate decision by the user, not something Predbat does for you. `password_legacy` is still sent over TLS, but without the additional RSA encryption layer, so it exists only for regions whose API still serves the older login
- Sunsynk is mode-less like DEYE/Enphase: Predbat only owns the charge window, export window, reserve and target SOCs - the component derives the internal Sunsynk work mode automatically from that intent
- There is a single whole-object settings endpoint, so every write is a read-modify-write of the entire settings object rather than a partial update. Predbat re-reads the settings immediately before every write to keep the race window as small as possible, and logs when a field it does not own has changed since the last read
- Using the Sunsynk phone app while Predbat is running can overwrite Predbat's settings, and vice versa - there is one whole-object write endpoint, so the last writer wins
- A write reaching the cloud does not mean the inverter has applied it: the dongle collects new settings on its next poll, typically one to five minutes later. Predbat tolerates a few cycles of divergence before warning

#### Configuration Options (sunsynk)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `username` | String | No | - | `sunsynk_username` | Your Sunsynk Connect account e-mail address (self-hosted add-on only) |
| `password` | String | No | - | `sunsynk_password` | Your Sunsynk Connect account password (self-hosted add-on only) |
| `key` | String | No | - | `sunsynk_key` | Injected OAuth access token (Predbat.com SaaS mode only) |
| `region` | String | No | `sunsynk` | `sunsynk_region` | API region: `sunsynk` (`api.sunsynk.net`) or `inteless` (`pv.inteless.com`) |
| `auth_method` | String | No | `password` | `sunsynk_auth_method` | `password` (RSA-encrypted login), `password_legacy` (pre-2025 plaintext login) or `oauth` (token injected by Predbat.com) |
| `token_expires_at` | String | No | - | `sunsynk_token_expires_at` | Injected OAuth token expiry timestamp (Predbat.com SaaS mode only) |
| `token_hash` | String | No | - | `sunsynk_token_hash` | Injected OAuth token refresh dedup handle (Predbat.com SaaS mode only) |
| `inverter_sn` | String/List | No | All found | `sunsynk_inverter_sn` | Restrict Predbat to specific inverter serial number(s) - single string or list |
| `automatic` | Boolean | No | false | `sunsynk_automatic` | Set to `true` to automatically configure Predbat to use the discovered Sunsynk inverter(s) (no manual apps.yaml sensor updates required) |
| `automatic_ignore_pv` | Boolean | No | false | `sunsynk_automatic_ignore_pv` | When `automatic` is enabled, set to `true` to prevent Sunsynk Cloud from overwriting the `pv_power` config |
| `control_enable` | Boolean | No | true | `sunsynk_control_enable` | Allow Predbat to write charge/export schedules to the inverter. Set to `false` for monitoring only |
| `battery_nominal_voltage` | Float | No | - | `sunsynk_battery_nominal_voltage` | Override for the battery pack's nominal voltage, only needed if it cannot be inferred from the reported charge target |

See [Sunsynk Cloud setup](inverter-setup.md#sunsynk-cloud) for the full walkthrough, including how to run the standalone diagnostics CLI.

---

### AlphaESS Cloud API (alphaess)

**Can be restarted:** Yes

#### What it does (alphaess)

Integrates with AlphaESS SMILE/Storion hybrid inverters via the AlphaESS Open API, providing cloud-based monitoring and timed charge control - no local Modbus/RS485 access is required. It cannot control export, see the notes below. Predbat discovers every battery system bound to the developer AppID, publishes monitoring sensors and derived ratings (including EV charger power and energy where one is fitted), and writes schedule control entities that map directly onto the AlphaESS charge and discharge schedule fields.

#### When to enable (alphaess)

- You have an AlphaESS hybrid inverter with battery storage registered on the AlphaESS cloud
- You want cloud-based monitoring, with optional battery control, and no local hardware access
- You have registered a developer account at <https://open.alphaess.com/> and have an AppID and AppSecret

#### Important notes (alphaess)

- **EXPERIMENTAL:** nobody on the Predbat project has AlphaESS hardware, so behaviour is inferred from AlphaESS's published Open API documentation and the Home Assistant AlphaESS integration rather than confirmed against real inverters. Every request and response is traced to the log (`api_debug`, on by default) with credentials redacted, so a tester can capture evidence for an issue report. Run the [diagnostics CLI](inverter-setup.md#alphaess-cloud) against your own system before trusting Predbat with control
- **Control is on by default**, the same as `sunsynk_control_enable`. Set `alphaess_control_enable: false` for monitoring only. `switch.predbat_set_read_only` additionally holds back Predbat's own automatic writes, including its periodic re-apply
- **Both write endpoints are documented as writable once per 24 hours.** Predbat therefore only writes when the payload actually changes, gates charge and discharge independently so one does not consume the other's budget, and paces writes with `alphaess_min_write_interval`
A schedule is committed in stages (window, then enable, then target SoC), so a small, capped number of corrections is allowed through within 60 seconds of a successful write - without it the inverter would run a schedule Predbat had already superseded until the interval expired
- **Export cannot be controlled at all.** The Open API has no forced-export, working-mode or dispatch endpoint, and AlphaESS document the discharge window as a *permission* window - during it the system runs in self-consumption, outside it the battery may only charge - so Force Export exports nothing beyond genuine solar surplus. Freeze Export is equally undeliverable, because nothing in the API stops the battery charging from solar. Set `select.predbat_mode` to `Control charge` on these systems, and see [AlphaESS cannot be used to control export](apps-yaml.md#alphaess-cannot-be-used-to-control-export)
- Predbat's controls map straight onto the schedule fields and the inverter does the timing. `batUseCap` carries the export target while an export window is programmed and the reserve otherwise, because the API has only one field for the discharge floor. A **zero** charge or discharge rate is how Predbat signals a freeze, since AlphaESS has no pause endpoint
- Times sit on a **15-minute grid** (`00:00` to `23:45`). Off-grid values are accepted by the API and then silently ignored by the inverter, so Predbat snaps windows inward and disables any window that snapping collapses
- Newer systems may be entitled to the periodic scheduling API (six windows a day, with a power setpoint per window). Predbat probes this once per system; a `6017` response means the account or hardware is not entitled and Predbat falls back to the universally available two-window endpoints. On that legacy path a non-zero charge rate is not honoured by the hardware
- **The API does not report a grid export limit.** On a G98/G99-capped site you must set `export_limit` in `apps.yaml` yourself, or Predbat will plan exports the connection clips. `battery_rate_max` is likewise not reported and is estimated from the inverter's nominal power - correct it with `battery_rate_max_scaling` or `alphaess_battery_rate_max`
- **An AlphaESS-connected EV charger is detected automatically.** `getLastPowerData` reports its per-charger power as `null` when no charger is fitted, which is the one documented signal that one physically exists. For a system that has one, Predbat publishes `ev_power` (W) and `ev_energy_today` (kWh) and, when `automatic` is enabled, points `car_charging_energy` at the charger energy of every system that has one. That mapping is inert on its own - Predbat only subtracts car energy from house load once `car_charging_hold` is enabled. Systems with no charger are left out, because their EV energy reads a permanent zero that is indistinguishable from a charger nobody uses. Controlling the charger is not supported
- Systems reporting no battery capacity (plug-in solar, such as the VT1000 family) are skipped by design - there is nothing for Predbat to control
- A system that does not serve live power data automatically falls back to five-minute history data and re-probes itself back to live if it recovers
- Using the AlphaESS phone app while Predbat is running can overwrite Predbat's settings, and vice versa - the charge and discharge endpoints are whole-object replacements, so the last writer wins. Predbat logs when a field it owns has changed since its last write
- A write reaching the cloud does not mean the inverter has applied it: it collects new settings on its next poll, typically one to five minutes later

#### Configuration Options (alphaess)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `app_id` | String | Yes | - | `alphaess_app_id` | Your AlphaESS developer AppID from <https://open.alphaess.com/> |
| `app_secret` | String | Yes | - | `alphaess_app_secret` | Your AlphaESS developer AppSecret |
| `inverter_sn` | String/List | No | All found | `alphaess_inverter_sn` | Restrict Predbat to specific system serial number(s) - single string or list |
| `automatic` | Boolean | No | false | `alphaess_automatic` | Set to `true` to automatically configure Predbat to use the discovered AlphaESS system(s) (no manual apps.yaml sensor updates required) |
| `automatic_ignore_pv` | Boolean | No | false | `alphaess_automatic_ignore_pv` | When `automatic` is enabled, set to `true` to prevent AlphaESS Cloud from overwriting the `pv_power` config |
| `control_enable` | Boolean | No | true | `alphaess_control_enable` | Allow Predbat to write charge/export schedules to the inverter. Set to `false` for monitoring only |
| `battery_rate_max` | Float | No | - | `alphaess_battery_rate_max` | Override, in Watts, for the battery's maximum charge/discharge rate. The API does not report one, so Predbat otherwise estimates it from the inverter's nominal power |
| `api_delay` | Float | No | 2 | `alphaess_api_delay` | Seconds to wait between API calls. AlphaESS advise a minimum 10-second polling interval |
| `min_write_interval` | Integer | No | 300 | `alphaess_min_write_interval` | Minimum spacing, in seconds, between writes to the same inverter and direction |

See [AlphaESS Cloud setup](inverter-setup.md#alphaess-cloud) for the full walkthrough, including how to run the standalone diagnostics CLI and how to bind or unbind a system.

---

### Alert Feed (alert_feed)

**Can be restarted:** Yes

#### What it does (alert_feed)

Monitors weather alert feeds (MeteoAlarm) for severe weather warnings that might impact your energy usage or solar generation. Predbat can use this information to adjust its planning accordingly.

#### When to enable (alert_feed)

- You want Predbat to be aware of weather alerts
- You want to adjust battery strategy based on weather warnings
- You're in an area with frequent severe weather

#### How it works (alert_feed)

- Checks for alerts every 30 minutes
- Processes weather warnings for your area
- Can be configured with custom alert URLs and filters in `apps.yaml`

#### Configuration Options (alert_feed)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `alert_config` | Dictionary | Yes | {} | `alerts` | Alert configuration including URL and filters |

#### Configuration example

See the [Weather Alert](apps-yaml.md#weather-alert-system) for more details

---

### Carbon Intensity API (carbon)

**Can be restarted:** Yes

#### What it does (carbon)

Retrieves current and forecast carbon intensity data for the UK electricity grid. This allows Predbat to make environmentally-conscious decisions, charging your battery when grid electricity is greener and discharging when it's more carbon-intensive.

#### When to enable (carbon)

- You want to minimize your carbon footprint
- You're interested in carbon-aware energy management
- You're in the UK (uses UK National Grid data)
- You want to see carbon intensity alongside cost optimisation

#### How it works (carbon)

- Uses your postcode to get regional carbon intensity data
- Provides both current intensity and forecasts
- Updates automatically throughout the day

Note: To use the carbon data in Predbat you also have to turn on **switch.predbat_carbon_enable**. If you want to optimise your plan for carbon then you also need to adjust the carbon weighting.

#### Configuration Options (carbon)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `postcode` | String | Yes | - | `carbon_postcode` | Your UK postcode for regional carbon intensity data |
| `automatic` | Boolean | No | false | `carbon_automatic` | Set to `true` to automatically point Predbat to the carbon data |

---

### Temperature API (temperature)

**Can be restarted:** Yes

#### What it does (temperature)

Fetches temperature forecasts from the Open-Meteo API to provide accurate temperature predictions for the next 48+ hours.
This temperature data is used by the ML Load Prediction component to improve load forecasting accuracy, especially for homes with electric heating or air conditioning systems.

#### When to enable (temperature)

- You are using ML Load Prediction and want improved accuracy
- Your energy consumption is significantly affected by temperature (heating/cooling)
- You want temperature forecasts available for other automations

#### How it works (temperature)

- Fetches temperature data from Open-Meteo API every hour
- Uses your location coordinates (from `temperature_latitude`/`temperature_longitude` or defaults to `zone.home`)
- Provides current temperature and hourly forecasts
- Publishes data to `sensor.predbat_temperature` with forecasts in the `results` attribute
- Automatically retries on API failures with exponential backoff

**Important**: This component is **recommended** when using ML Load Prediction, as temperature data can improve prediction accuracy for households with electric/heat-pump heating.

#### Configuration Options (temperature)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `temperature_enable` | Boolean | Yes | false | `temperature_enable` | Set to `true` to enable temperature forecasts |
| `temperature_url` | String | No | Open-Meteo API URL | `temperature_url` | API URL with LATITUDE/LONGITUDE placeholders |
| `temperature_latitude` | Float | No | Uses zone.home | `temperature_latitude` | Latitude for temperature forecast location |
| `temperature_longitude` | Float | No | Uses zone.home | `temperature_longitude` | Longitude for temperature forecast location |

#### Configuration example (temperature)

```yaml
predbat:
  # Enable temperature forecasts (recommended for ML load prediction)
  temperature_enable: true

  # Optional: specify location (defaults to zone.home)
  # temperature_latitude: 51.5074
  # temperature_longitude: -0.1278
```

#### Accessing temperature data (temperature)

Temperature data is published to:

- `sensor.predbat_temperature` - Current temperature with forecast in `results` attribute

The `results` attribute contains a dictionary of timestamp strings (ISO format with timezone) to temperature values in °C.

---

### Kraken Energy (kraken)

**Can be restarted:** Yes

#### What it does (kraken)

Connects to the Kraken GraphQL API used by **EDF** and **E.ON Next** to automatically discover your electricity tariff and download import (and optionally export) rates. Once the tariff is discovered, rates are fetched from the provider's public REST API and wired directly into Predbat — no manual rate configuration needed.

On first run the component queries your account for active meter point agreements, identifies the correct tariff code and product code, then polls for fresh rates every 10 minutes and re-checks the tariff every 30 minutes so any mid-contract tariff changes are picked up automatically.

#### When to enable (kraken)

- You are an EDF or E.ON Next customer in the UK
- You want Predbat to automatically track your tariff rates (including time-of-use and half-hourly tariffs)
- You want export rates fetched automatically if you have a solar/battery export arrangement

#### Important notes (kraken)

- **EDF and E.ON Next only** — this component uses the Kraken GraphQL schema specific to those providers and will not work with Octopus Energy (use the `octopus` component instead)
- The component automatically sets `metric_octopus_import` and `metric_standing_charge` in Predbat, and will also set `metric_octopus_export` **when an export tariff is discovered** — no manual `apps.yaml` edits are needed for those settings once the relevant tariffs have been detected and the component is running
- E.ON Next customers who have solar export may have their import and export on **separate account numbers** — in this case the component will attempt to discover the export account automatically via an address-matching strategy, or you can provide `export_account_id` explicitly
- For OSS (self-hosted) installations you need to supply credentials — either an API key (`api_key` auth method) or email/password (`email` auth method). SaaS/cloud-managed installations use OAuth and have credentials managed automatically. If you can't find a way to generate a separate API key for your provider, use the `email` auth method with the same email/password you use to sign into your normal EDF or E.ON Next online account — no separate key is required for that method
- For accounts with a SmartFlex-managed EV device, an `intelligent_dispatch` binary sensor is published per device (see [Published entities](#published-entities-kraken)) and automatically wired into `octopus_intelligent_slot`, exactly like Octopus Intelligent Go — no manual `apps.yaml` configuration is needed for this. Make sure **switch.predbat_octopus_intelligent_charging** (see [car charging docs](car-charging.md)) is turned On so Predbat actually uses the dispatch slots for planning

#### Configuration Options (kraken)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `provider` | String | Yes | - | `kraken_provider` | Energy provider: `edf` or `eon` |
| `account_id` | String | Yes | - | `kraken_account_id` | Your account number (shown on your bill or online account dashboard) |
| `auth_method` | String | No | `oauth` | `kraken_auth_method` | Authentication method: `oauth` (SaaS/managed), `api_key`, or `email` |
| `key` | String | No | - | `kraken_key` | API key — required when `auth_method` is `api_key` |
| `email` | String | No | - | `kraken_email` | Account email — required when `auth_method` is `email` |
| `password` | String | No | - | `kraken_password` | Account password — required when `auth_method` is `email` |
| `mpan` | String | No | Auto | `kraken_mpan` | Preferred import MPAN — only needed if you have multiple meter points and the wrong one is selected |
| `export_account_id` | String | No | Auto | `kraken_export_account_id` | Export account number — only needed for E.ON Next customers where import and export are on separate accounts and auto-discovery fails |
| `export_mpan` | String | No | Auto | `kraken_export_mpan` | Preferred export MPAN — only needed if auto-discovery selects the wrong export meter point |
| `base_url` | String | No | Auto | `kraken_base_url` | Override the API base URL — advanced use only (e.g. proxying GraphQL calls) |

**Security note:** Store credentials in `secrets.yaml`:

```yaml
# secrets.yaml
kraken_key: YOUR_REAL_API_KEY
kraken_password: YOUR_REAL_PASSWORD
```

#### apps.yaml configuration example (kraken)

**EDF — API key authentication:**

```yaml
  kraken_provider: edf
  kraken_account_id: A-12345678
  kraken_auth_method: api_key
  kraken_key: !secret kraken_key
```

**E.ON Next — email/password authentication:**

```yaml
  kraken_provider: eon
  kraken_account_id: A-12345678
  kraken_auth_method: email
  kraken_email: myemail@example.com
  kraken_password: !secret kraken_password
```

**E.ON Next — with explicit export account (split accounts):**

```yaml
  kraken_provider: eon
  kraken_account_id: A-12345678        # import account
  kraken_export_account_id: A-87654321 # export account (different number)
  kraken_auth_method: email
  kraken_email: myemail@example.com
  kraken_password: !secret kraken_password
```

#### How to find your account number (kraken)

Your account number is displayed on your bill and in your online account dashboard. It typically starts with `A-` followed by 8 digits.

#### Published entities (kraken)

All entities use the pattern `sensor.predbat_kraken_{account_id}_{suffix}` (account number with hyphens replaced by underscores). For example, account `A-12345678` produces:

| Entity | Description |
| ------ | ----------- |
| `sensor.predbat_kraken_a_12345678_account_status` | Connection status: `discovering`, `connected`, or `error` |
| `sensor.predbat_kraken_a_12345678_tariff_code` | Active import tariff code (e.g. `E-1R-FLEX-22-11-25-A`) |
| `sensor.predbat_kraken_a_12345678_import_rates` | Import rate periods — consumed by Predbat automatically |
| `sensor.predbat_kraken_a_12345678_import_standing` | Daily standing charge in £/day |
| `sensor.predbat_kraken_a_12345678_export_rates` | Export rate periods — only present when an export tariff is found |
| `binary_sensor.predbat_kraken_a_12345678_intelligent_dispatch[_N]` | On when a SmartFlex dispatch slot is active for device `N` — only present for accounts with a SmartFlex-managed EV device |

Predbat is automatically configured to use these energy rates once Kraken is enabled.

---

### ML Load Prediction (load_ml)

**Can be restarted:** Yes

#### What it does (load_ml)

Uses a neural network to predict your household energy consumption for the next 48 hours based on historical patterns, time-of-day, day-of-week, and optionally temperature and PV generation data.
This provides more accurate load predictions than simple averaging, especially for households with variable usage patterns.

#### When to enable (load_ml)

- You want more accurate load predictions than historical averages
- Your energy consumption has regular daily/weekly patterns
- You have at least 1 day of historical load data (7+ days recommended)
- You want Predbat to automatically adapt to changing consumption patterns

#### How it works (load_ml)

- Fetches historical load data from your configured `load_today` sensor
- Optionally incorporates PV generation and temperature forecast data
- Trains a multi-layer neural network on your historical patterns
- Makes autoregressive predictions for 48 hours ahead in 5-minute intervals
- Fine-tunes periodically (every 2 hours) to adapt to changing patterns
- Validates predictions and falls back gracefully if accuracy is poor
- Publishes predictions to `sensor.predbat_load_ml_forecast`

**Important**: For best results, enable the Temperature component (`temperature_enable: true`) as temperature data significantly improves prediction accuracy.

For a detailed explanation of how the neural network works and comprehensive configuration guidance, see the [ML Load Prediction documentation](load-ml.md).

#### Configuration Options (load_ml)

| Option | Type | Required | Default | Config Key | Description |
| ------ | ---- | -------- | ------- | ---------- | ----------- |
| `load_ml_enable` | Boolean | Yes | false | `load_ml_enable` | Set to `true` to enable ML load prediction |
| `load_ml_source` | Boolean | No | false | `load_ml_source` | Set to `true` to use ML predictions in Predbat battery planning |
| `load_ml_max_days_history` | Integer | No | 28 | `load_ml_max_days_history` | Maximum days of load history to fetch from HA on each poll (bounded by HA recorder retention) |
| `load_ml_database_days` | Integer | No | 90 | `load_ml_database_days` | Days of history to accumulate in the on-disk database (`predbat_ml_history.npz`); set to 0 to disable the database |

Note: `load_today`, `pv_today` and `car_charging_energy` apps.yaml configuration items are also used, but these should already be set in Predbat.

#### Configuration example (load_ml)

```yaml
predbat:
  # Enable ML load prediction
  load_ml_enable: true
  # Use the data in Predbat, can be false while exploring the predictions but not using them
  load_ml_source: true

  # Optional: days of history to fetch from HA each poll (default 28, limited by HA retention)
  # load_ml_max_days_history: 28

  # Optional: days of history to accumulate on disk for training (default 90)
  # load_ml_database_days: 90

  # Optional but recommended: enable temperature forecasts
  temperature_enable: true
```

#### Understanding model status (load_ml)

The ML component tracks several status indicators:

- **not_initialized**: Model has not been created yet
- **training**: Model is currently training on historical data
- **active**: Model is trained and making predictions
- **validation_failed**: Predictions are disabled due to high validation error
- **stale**: Model hasn't been trained in 48+ hours and needs retraining

Check Predbat logs for training progress and validation metrics:

```text
ML Component: Starting initial training
ML Predictor: Training complete, final val_mae=0.3245 kWh
ML Component: Model status: active
```

#### Accessing predictions (load_ml)

ML load predictions are published to:

- `sensor.predbat_load_ml_forecast` - Contains 48-hour prediction in `results` attribute

Predbat automatically uses these predictions when making battery charge/discharge decisions.

#### For more information (load_ml)

See the comprehensive [ML Load Prediction documentation](load-ml.md) for:

- Detailed explanation of neural network architecture
- Training process and parameters
- Expected accuracy metrics
- Troubleshooting guide
- Advanced configuration options

---

## Managing Components

### Checking Component Status

You can check the status of all components through the web interface:

1. Open the web interface (default: `http://your-ha-ip:5052`)
2. Navigate to the **Components** page (`/components`)
3. View the status of each component:
   - **Enabled/Disabled** - Whether the component is configured
   - **Running/Stopped** - Current operational status
   - **Healthy/Unhealthy** - Whether the component is working correctly
   - **Last Updated** - When the component last successfully updated

### Restarting Components

Most components can be restarted if they encounter problems:

- Use the 'Restart' button on the Components page in the web interface
- Or restart Predbat entirely to restart all components

**Note:** Core components (Database Manager, Home Assistant Interface, and Home Assistant History) cannot be restarted individually and require a full Predbat restart.

### Editing Component Configuration

Click the Pencil icon next to a component to change the component configuration, e.g.:

![image](images/web-interface-component-configuration.png)

Once you save the component configuration the changes will be written to `apps.yaml` and Predbat and all components will be restarted.

### Component Health

A component is considered healthy when:

- Its task is running
- It has updated within the last 60 minutes
- It reports no errors

If a component becomes unhealthy:

1. Check your configuration in `apps.yaml`
2. Verify API keys and credentials are correct
3. Check network connectivity
4. Review Predbat logs for error messages
5. Try restarting the component

---
