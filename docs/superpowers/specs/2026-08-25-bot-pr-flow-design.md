# Bot PR Flow — Design

Date: 2026-08-25
Status: Approved for implementation planning

## 1. Purpose

Extend the existing issue-triage bot (`tools/triage_daemon.py` + `.claude/skills/issue-triage/`)
with a second, opt-in flow: when a maintainer adds the `BOT_PR` label to an already-triaged
issue, the bot implements the fix/feature described in the ticket and opens a **draft** PR
referencing it, with `springfall2008` set as assignee.

Today the bot is strictly read/comment-only — `DISALLOWED_TOOLS` explicitly blocks
`git push`, `git commit`, and `gh pr create`. This adds a genuinely new capability
(autonomous code changes + PR creation), gated behind a label a human deliberately adds,
so it stays a single, opt-in trigger per issue rather than an always-on behaviour.

Out of scope: auto-merging, closing issues, editing PRs after creation, and running on
anything other than `springfall2008/batpred` issues.

## 2. Labels

Four labels, none of which exist in the repo today:

| Label | Applied by | Meaning |
|---|---|---|
| `BOT_TRIAGED` | `issue-triage` skill, step 9 (new) | This issue has been through the triage bot — applied every time triage completes, including duplicate-closes. `BOT_` prefix to match `BOT_PR` and avoid colliding with an unrelated "triaged" label someone else might already use. |
| `BOT_PR` | Maintainer (manual) | Trigger: implement this ticket and open a draft PR. |
| `BOT_PR_OPENED` | `issue-pr` skill (new), on success | The PR flow completed; swapped in for `BOT_PR` so the daemon doesn't reprocess it. |
| `BOT_PR_FAILED` | `issue-pr` skill (new), on failure | The PR flow's quality gate didn't pass; swapped in for `BOT_PR` so the daemon doesn't retry every poll. Re-add `BOT_PR` once the blocker is addressed. |

Create with:

```bash
gh label create BOT_TRIAGED --repo springfall2008/batpred --color 0e8a16 --description "Has been through the triage bot"
gh label create BOT_PR --repo springfall2008/batpred --color 5319e7 --description "Trigger: bot should implement this ticket and open a draft PR"
gh label create BOT_PR_OPENED --repo springfall2008/batpred --color 0e8a16 --description "Bot PR flow opened a draft PR for this ticket"
gh label create BOT_PR_FAILED --repo springfall2008/batpred --color d93f0b --description "Bot PR flow's quality gate failed; see the comment for details"
```

### 2.1 Backfill for pre-existing triaged issues

`BOT_TRIAGED` only starts getting applied going forward, so an issue triaged before this
change has a bot triage comment but no label. The precondition check (below) therefore
checks the label first and falls back to scanning comments for the triage disclosure
line if the label is absent. When the fallback finds a comment, it backfills the
`BOT_TRIAGED` label onto the issue at that point — no separate migration script; the label
set converges to consistent state the first time each old issue is touched.

## 3. Modification to the existing `issue-triage` skill

One addition to `.claude/skills/issue-triage/SKILL.md` step 9 (posting the comment):
after posting, apply the `BOT_TRIAGED` label. This runs on the existing, already-live
triage path for every new issue, not just ones later tagged `BOT_PR` — it is the
mechanism that keeps the label current going forward. No other change to that skill;
its permission set and read-only guarantee are unchanged, and no permission-set edit
is needed for this addition either — the existing `Bash(gh *)` allow entry already
covers `gh issue edit --add-label`, and only specific subcommands (`gh pr create`,
`gh pr merge`, etc.) are carved back out via `DISALLOWED_TOOLS`.

## 4. Daemon changes (`tools/triage_daemon.py`)

Each poll cycle, alongside the existing `fetch_new_issues()` call:

```python
def fetch_bot_pr_issues():
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--state", "open",
         "--label", "BOT_PR", "--json", "number,labels"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)
```

For each issue returned:

1. `sync_repo()` / `reset_scratch()` (unchanged, same as today).
2. Duplicate-work guard: `gh pr list --search "\"Fixes #<issue-number>\" in:body" --state all` —
   searches for the exact phrase the skill always includes in its PR body (Section 5, step 6)
   rather than a bare number, which could false-positive on an unrelated PR that happens to
   mention the issue number. If a match exists, skip. Protects against reprocessing if the
   daemon crashed between push and label-swap on a previous attempt.
3. Precondition check — `BOT_TRIAGED` in the label list already returned by
   `fetch_bot_pr_issues()` (no extra API call). If absent, fall back to
   `gh issue view <number> --json comments` and scan for the triage disclosure line;
   if found, backfill `BOT_TRIAGED` via `gh issue edit <number> --add-label BOT_TRIAGED`.
   If neither, run `/issue-triage <number>` first (existing skill, existing permissions,
   unchanged).
4. Run `/issue-pr <number>` under the new permission set (Section 6).
5. Check `has_existing_pr(<number>)` again afterwards — this is how success is judged,
   *not* the invocation's exit code: a Claude Code session that completes normally still
   exits 0 whether it opened a PR or decided the quality gate failed and posted a comment
   instead, so the exit code alone can't distinguish the two outcomes.
6. If a PR now exists: `gh issue edit <number> --remove-label BOT_PR --add-label BOT_PR_OPENED`.
   If not: `gh issue edit <number> --remove-label BOT_PR --add-label BOT_PR_FAILED` — the
   `issue-pr` skill itself posts the explanatory comment before stopping (Section 5, step 7);
   the daemon only swaps the label.

State tracking is entirely label-based; no new fields in `state.json`.

## 5. New skill: `.claude/skills/issue-pr/SKILL.md`

Structured like `issue-triage/SKILL.md`. Arguments: `<issue-number> [scratch=<dir>]`,
same convention as the triage skill.

1. **Read the ticket** — issue body plus the existing bot triage comment (classification,
   priority, root-cause pointer).
2. **Investigate** — sync check (`git fetch origin main && git reset --hard origin/main
   && git clean -fd`), read `.claude/skills/issue-triage/references/debug-journal.md`
   (shared, not duplicated) and the relevant source area, `git log`/`git blame` for
   recent related changes the reporter's version might not have.
3. **Implement** — write the fix/feature per `CLAUDE.md` (docstrings, `lower_case_with_underscores`
   naming, 256-char line length), and add/update a unit test — `CLAUDE.md` requires unit
   tests for all new code, and `CLAUDE.md` is picked up automatically since Claude Code
   reads it from the working directory with no extra wiring.
4. **Quality gate** — run `./run_pre_commit` and the relevant test module via
   `tools/triage_test.sh <name>` (or `./run_all --quick` if there's no clean single-module
   mapping). Both must pass before continuing to step 5. If either fails, stop, do not
   push or open a PR, and go to step 7.
5. **Branch, commit, push** — branch named `fix/<slug>-<issue#>` or `feat/<slug>-<issue#>`
   (prefix from the triage classification), matching existing repo convention (e.g.
   `fix/solis-tou-bit-refused-4707`).
6. **Open the draft PR** —
   `gh pr create --draft --assignee springfall2008 --title "..." --body "..."`. Body
   includes: a disclosure line ("automated draft PR generated from issue #N — needs
   maintainer review before merging"), `Fixes #N`, a summary, what was run in step 4 and
   its result, and a debug-journal reference if one was used. Report success back to the
   daemon.
7. **On failure (from step 4, or any earlier step that can't proceed)** — post one comment
   on the issue via `gh issue comment` explaining what was attempted and what failed
   (same "say plainly what you could not do" guardrail as the triage skill), then stop.
   No branch is pushed, no PR is opened — the daemon detects this outcome itself by
   checking for a PR afterwards, so there's nothing else this step needs to signal.

## 6. Permission model

A new `ALLOWED_TOOLS`/`DISALLOWED_TOOLS` pair, used **only** for the `/issue-pr`
invocation. The existing `/issue-triage` invocation and its permissions are unchanged.

Adds to the existing read-only allowlist (`git checkout*` is already allowed today and
covers `git checkout -b`, so no new entry is needed for branch creation):

```
Bash(git add*)
Bash(git commit*)
Bash(git push*)
Bash(gh pr create*)
Bash(./run_pre_commit*)
Bash(./run_pre_commit)
```

`gh issue edit` is deliberately *not* added here — the label swaps in Section 4 steps
5–6 are done by the daemon's own Python code via `subprocess`, the same trust boundary
as `sync_repo()`/`save_state()` today, not by the model inside the Claude Code session.
Keeping that bookkeeping deterministic and outside the sandboxed session means a label
swap can't be skipped by a model that forgets a step.

Keeps blocking (same rationale as today — deny wins over the broad `Bash(gh *)` /
`Bash(git ...)` allows): `gh pr merge`, `gh pr close`, `gh repo*`, `gh release*`,
`gh workflow*`, `gh auth*`, `gh secret*`, `gh api*`, all `mcp__*`. `git push` is only
ever invoked by the skill against a new `fix/`/`feat/` branch, never `main`, but nothing
in the tool-permission layer enforces that — it relies on the skill instructions and
draft-PR review before merge, same trust model as everything else in this daemon.

`--max-turns 150` and `--max-budget-usd 25.00` (up from the triage flow's 60 / $10) —
writing code, running pre-commit, and running tests is heavier than read-only
investigation. Starting values; tune after the first few real runs.

### 6.1 Infra prerequisite

Whatever GitHub identity the daemon's `gh auth` already uses needs **push access** to
`springfall2008/batpred`. Today that identity only ever reads and comments, so this has
never been exercised — confirm (or grant) push/write access before enabling the `BOT_PR`
label in the repo.

No separate bot account is required: the PR uses `--assignee springfall2008` rather than
`--reviewer springfall2008`. GitHub blocks requesting a review from a PR's own author
(so `--reviewer` would need a distinct identity from whichever account authors the PR),
but self-assignment is allowed, so the daemon can run as `springfall2008` itself.

## 7. Testing

### 7.1 `triage_daemon.py` unit tests

New file `tools/test_triage_daemon.py`, stdlib `unittest` + `unittest.mock` (no new
dependency — `triage_daemon.py` itself only imports stdlib). It does **not** go through
`coverage/run_all`/`TEST_REGISTRY`: every existing entry there imports `PredBat`/
`TestHAInterface` from `apps/predbat`, and `triage_daemon.py` has no dependency on that
app — folding it in would be a layering violation. Run directly with
`python3 tools/test_triage_daemon.py`.

Covers, with `subprocess.run` mocked (no real `gh`/`git`/`claude` calls):

- `load_state()`/`save_state()` round-trip, including the corrupt-JSON fallback.
- `fetch_new_issues()` and `fetch_bot_pr_issues()` parsing of `gh` JSON output.
- The `BOT_PR` precondition check (Section 4 step 3): label present → proceeds directly;
  label absent + comment found → backfills `BOT_TRIAGED` and proceeds; neither → triggers
  `/issue-triage` first.
- The duplicate-work guard's search query construction (Section 4 step 2) — asserts it
  searches the quoted `"Fixes #<N>"` phrase, not a bare number.
- The success/failure label-swap calls (`BOT_PR` → `BOT_PR_OPENED` / `BOT_PR_FAILED`),
  driven by the post-hoc `has_existing_pr()` check described in Section 4 step 5, not by
  the `/issue-pr` invocation's exit code.
- A regression test asserting the exact `ALLOWED_TOOLS`/`DISALLOWED_TOOLS` delta between
  the triage invocation and the new `/issue-pr` invocation (Section 6) — specifically that
  `gh pr merge`, `gh pr close`, `gh repo*`, `gh release*`, `gh workflow*`, `gh auth*`,
  `gh secret*`, `gh api*`, and all `mcp__*` stay denied for the PR flow too. This is the
  one test worth never letting rot silently, since it's the thing standing between "opens
  a draft PR" and "can merge/close/administer the repo."

### 7.2 Wiring into pre-commit

A new local hook in `.pre-commit-config.yaml`, following the existing `cspell-dictionary-sorter`
pattern, scoped to only run when the daemon or its tests change:

```yaml
- id: triage-daemon-tests
  name: triage daemon unit tests
  language: python
  entry: python tools/test_triage_daemon.py
  files: ^tools/(triage_daemon\.py|test_triage_daemon\.py)$
  pass_filenames: false
```

This makes it run both via local `./run_pre_commit` and the existing `pre-commit/action`
step in `code-quality.yml` — no new CI job needed.

### 7.3 End-to-end verification

Beyond the unit tests, a manual dry run against a real triaged issue: add `BOT_PR`, watch
the daemon log, confirm the branch/commit/PR look right end-to-end, then confirm the
`BOT_PR_FAILED` path separately by pointing it at a ticket engineered to fail the quality
gate. Unit tests cover the daemon's decision logic; they can't substitute for seeing a
real `gh pr create --draft` succeed.

## 8. Error handling summary

| Situation | Behaviour |
|---|---|
| `BOT_PR` added, issue not yet triaged (no label, no comment) | Daemon runs `/issue-triage` first, then proceeds. |
| `BOT_PR` added, old issue triaged before labels existed | Fallback comment-scan finds it, backfills `BOT_TRIAGED`, proceeds. |
| Daemon crashes between push and label-swap | Next poll's `gh pr list` guard finds the existing PR and skips reprocessing. |
| Implementation fails to pass pre-commit/tests | No push, no PR; one comment posted; label swapped to `BOT_PR_FAILED`. |
| `issue-pr` invocation errors outright (crash, budget/turn cap) | Same as above — no PR exists afterwards either way, so the daemon's post-hoc `has_existing_pr()` check swaps to `BOT_PR_FAILED` regardless of the invocation's exit code. |
