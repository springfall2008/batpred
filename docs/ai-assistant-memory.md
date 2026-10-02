# AI assistant memory for Predbat development

Several Predbat contributors use [Claude Code](https://claude.com/claude-code) to work on the codebase. This page explains how to give it a
persistent memory across sessions and repositories, and what to put in your own `CLAUDE.md` so it actually uses that memory.

None of this is required to build, test or contribute to Predbat. The repository's own `CLAUDE.md` works without any of it.

## Which server does what

Two MCP servers are used in Predbat development. They do different jobs:

| Server | What it is | What it remembers |
|--------|------------|-------------------|
| [agent-memory-mcp](https://github.com/adamrdrew/agent-memory-mcp) (`@adamrdrew/agent-memory-mcp`) | A local memory store with hybrid keyword and vector search, backed by LanceDB | What you are working on and what you have learnt: task status, root causes, design decisions |
| [GitNexus](https://github.com/abhigyanpatwari/GitNexus) | A code knowledge graph: symbols, call graph, execution flows, impact analysis | The structure of the code. The repository `CLAUDE.md` already tells Claude how to use it |

This page covers **agent-memory-mcp**, the store that the `CLAUDE.md` guidance below relies on.

## Setting up agent-memory-mcp

### Prerequisites

- Node.js 18 or later.
- Claude Code.
- About 80 MB of disk for the embedding model, which is downloaded once at install time. After that the server needs no network access and no API key.
  All data stays on your machine.

### Install

Install the package globally:

```bash
npm install -g @adamrdrew/agent-memory-mcp
```

Then register it with Claude Code at **user** scope, so it is available in every repository and every session rather than only in this one:

```bash
claude mcp add --scope user agent-memory -e MEMORY_DB_PATH="$HOME/agent-memory-db" -- agent-memory-mcp
```

`MEMORY_DB_PATH` is the directory holding the LanceDB database, and it is the only required setting. Put it **outside** any repository:
the store is plaintext on disk and must never be committed.

Start a new Claude Code session and run `/mcp`, or run `claude mcp list` from a shell. `agent-memory` should show as connected, and
Claude gains the tools `store`, `store_batch`, `search`, `recall`, `find_related`, `list_recent`, `update`, `delete` and `stats`.

### Optional settings

Pass any of these with further `-e` flags on the `claude mcp add` command:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MEMORY_DECAY_HALF_LIFE` | `30` | Days until a memory's search score halves, so recent memories rank above old ones of similar relevance. `0` disables decay. Memories tagged `evergreen` or `never-forget` never decay |
| `ENABLE_HARDCOPY` | off | Set to `true` to mirror every change to JSON files, which gives you a human-readable backup |
| `HARDCOPY_PATH` | none | Directory for those JSON files. Required when `ENABLE_HARDCOPY` is `true` |
| `EMBEDDING_MODEL` | `Xenova/all-MiniLM-L6-v2` | HuggingFace model used for embeddings |

### Upgrading

```bash
npm update -g @adamrdrew/agent-memory-mcp
```

After upgrading, re-check the `update` trap described below. It is recorded against version 1.1.2.

## What to put in your CLAUDE.md

Installing the server only makes the tools available. Claude will not use them consistently unless your instructions say when to read
and when to write. Put these instructions in your **user-level** file, `~/.claude/CLAUDE.md`, not in the repository's `CLAUDE.md`.
The server is part of your personal setup, and the repository file is shared with contributors who may not have it installed.

Copy the block below into `~/.claude/CLAUDE.md` and adjust it to suit you. It is written for any repository. The `repo:batpred` tag and
the debug journal are the Predbat-specific parts.

```markdown
## Persistent memory (agent-memory MCP)

The `agent-memory` MCP server is registered at user scope, so it is available in every repo and
every session. It is the cross-repo record of **what I am working on** and **what I have learnt**.
Use it, in preference to re-deriving context from git history.

### At the start of a session

When picking up work, or whenever asked "what's left", "where was I", "what's outstanding" — call
`recall` with the relevant topics before answering. Do not answer from the conversation alone;
these questions are precisely the ones whose answer lives in an earlier session.

To sweep across every repo, search by status tag rather than by topic:
`search(query="outstanding work", tags=["status:open", "status:blocked", "status:in-review"])`.

### Recording work status

Store an entry when starting a task, reviewing a PR or ticket, or parking something unfinished.
Use category `observation` and always carry these tags:

- `repo:<name>` — the repository, e.g. `repo:batpred`. **Always.** Without it a cross-repo query
  cannot tell whose work it is.
- `status:open` | `status:in-review` | `status:blocked` | `status:done` — exactly one.
- `pr:<number>` / `issue:<number>` — when the work has one.

Write the content so it stands alone in six months: what the work is, where it got to, and what
the next action is. "Fixed the bug" is useless; name the branch, the file, the decision taken and
what remains.

### Keeping status true — the rule that makes this work

When a task's state changes, **`update` the existing memory; never store a second one.** Find it
first (`search` by `pr:`/`issue:`/`repo:` tag), then update its content and swap the `status:` tag.

Two entries for one PR is how "what's left to do" starts returning work that shipped weeks ago.
A store that lies about status is worse than no store, because it is believed. If a duplicate has
crept in, `delete` the stale one rather than leaving both.

> **`update` is destructive if you omit `content`.** Calling `update` with only `tags` (or only
> `category`) fails with `Found field not in schema: vector.isValid` **and deletes the row**.
> **Always pass the full `content` on every `update` call**, re-sending the existing text
> unchanged when only the tags are meant to change.

### Recording technical knowledge

Store durable findings — an architecture decision and its reasoning, a non-obvious constraint, a
trap that cost real time. Use the category that fits (`architecture`, `bug-fix`, `debugging`,
`performance`, `security`, `learning`, `tool-usage`), and tag `repo:<name>` plus topic tags.

Store the *why*, not the diff. Git already has the diff. Worth storing: what was tried and
rejected, why a design is the shape it is, what a failure's root cause turned out to be.

Do not store: secrets, tokens, API keys, credentials, or anything pasted from a `.env` — the store
is plaintext on disk. Do not store what the repo already records (file structure, commit history,
what CLAUDE.md says). Do not store one-off session chatter.

## The debug journal

In batpred, `tools/debug-journal.md` records debugging investigations and how they turned out:
per-integration API quirks, symptom-to-module pointers, and traps that cost real time.
Read it before debugging an integration or a "the plan is wrong" report, and add to it when an
investigation concludes. Its entries describe past investigations, not current main — treat them
as "look here first" and confirm against the working tree.

## Keeping the stores in their lanes

- **agent-memory** — what is still open anywhere, plus durable technical knowledge.
- **The repo's debug journal** — why an integration misbehaves; shared with every contributor.
- **`~/.claude/projects/*/memory/`** — Claude Code's built-in notes about me and my preferences.

When agent-memory and the journal disagree, agent-memory wins for task status and the journal wins
for debugging detail. Fix the one that is wrong rather than reporting both.
```

### Why the instructions are shaped this way

- **Status tags, not topics, answer "what's outstanding?"** A topic search returns whatever is semantically close, including work that
  finished weeks ago. Filtering on `status:open`, `status:blocked` and `status:in-review` returns only live work, across every repository.
- **One memory per task, updated in place.** Every duplicate is a stale answer waiting to be returned. The instructions tell Claude to
  search for the existing entry before it writes.
- **The `update` trap.** In agent-memory-mcp 1.1.2, calling `update` with only `tags` or only `category` fails with
  `Found field not in schema: vector.isValid` and deletes the memory. Passing `content` re-embeds the entry and takes the code path that
  works. Keep this warning in your instructions until you have confirmed that an upgrade fixes it.
- **The debug journal is shared, and agent-memory is personal.** A finding that would help the next person triaging an issue belongs in
  `tools/debug-journal.md`, where everyone can read it. agent-memory holds your own task status and notes.

### Tag conventions

| Tag | When | Example |
|-----|------|---------|
| `repo:<name>` | Always | `repo:batpred` |
| `status:<state>` | Work-status entries: exactly one of `open`, `in-review`, `blocked`, `done` | `status:in-review` |
| `pr:<number>` | Work tied to a pull request | `pr:5250` |
| `issue:<number>` | Work tied to an issue | `issue:5079` |
| Topic tags | Knowledge entries | `octopus`, `solis`, `prediction-kernel` |
| `evergreen` | Knowledge that should never decay in search ranking | |
