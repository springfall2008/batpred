# Bot PR Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Tasks 12 and 13 are explicitly **not** for automated execution — see their headers.

**Goal:** Extend the issue-triage bot so that adding the `BOT_PR` label to an already-triaged GitHub issue makes it implement the fix/feature and open a draft PR for maintainer review.

**Architecture:** `tools/triage_daemon.py` gains a second poll (issues labelled `BOT_PR`) alongside its existing new-issue poll, orchestrating a precondition check, a duplicate-work guard, and a new `/issue-pr` skill invocation under a separate, more permissive tool allowlist. State lives entirely in GitHub labels (`BOT_TRIAGED`, `BOT_PR_OPENED`, `BOT_PR_FAILED`) — no new fields in `state.json`. Success/failure is judged by re-checking for the PR afterwards, not by the invocation's exit code.

**Tech Stack:** Python 3 stdlib (`subprocess`, `json`, `pathlib`, `unittest`, `unittest.mock`), `gh` CLI, Claude Code CLI (`claude -p`), pre-commit.

**Spec:** `docs/superpowers/specs/2026-08-25-bot-pr-flow-design.md`

## Global Constraints

- Line length: 256 chars (Black), 250 chars (Flake8).
- Docstrings: one-line docstring on every new function (100% coverage required per CLAUDE.md's `interrogate` policy).
- British English spelling in all prose/comments/docstrings (e.g. "labelled" not "labeled", "behaviour" not "behavior") — this repo enforces `en-gb` via CSpell.
- Variable naming: `lower_case_with_underscores`.
- Unit tests required for all new code (CLAUDE.md).
- `tools/triage_daemon.py` and its test file have no dependency on `apps/predbat` — do not import from it, and do not register the new tests in `TEST_REGISTRY`/`run_all`.
- Every `subprocess.run` call in `tools/triage_daemon.py` uses `check=True` unless the call's own failure is meaningful to the caller (matches the existing pattern in the file).

---

## Task 1: Apply `BOT_TRIAGED` label from the existing triage skill

**Files:**
- Modify: `.claude/skills/issue-triage/SKILL.md:91-95` (step 9)

**Interfaces:**
- Consumes: nothing new.
- Produces: every future triage run applies the `BOT_TRIAGED` label — later tasks' `is_already_triaged()` (Task 4) depends on this label existing on newly-triaged issues.

- [ ] **Step 1: Edit step 9 of the skill**

Find this text in `.claude/skills/issue-triage/SKILL.md`:

```markdown
## 9. Post one comment

Check existing comments first — if one from you is already there, stop; don't post again.

Post exactly one comment via `gh issue comment <number> --body "..."`, opening with a line disclosing this is an automated first-pass triage (a maintainer will review before any action is taken), followed by: classification, priority (if set), what you investigated and found (including test result if you ran one), a root-cause pointer if you have one, and any information request.
```

Replace it with:

```markdown
## 9. Post one comment

Check existing comments first — if one from you is already there, stop; don't post again.

Post exactly one comment via `gh issue comment <number> --body "..."`, opening with a line disclosing this is an automated first-pass triage (a maintainer will review before any action is taken), followed by: classification, priority (if set), what you investigated and found (including test result if you ran one), a root-cause pointer if you have one, and any information request.

After posting, apply the `BOT_TRIAGED` label via `gh issue edit <number> --add-label BOT_TRIAGED` — every triage run gets this label, including a duplicate-close, regardless of classification. It marks the issue as triaged for the separate PR-creation flow (see `.claude/skills/issue-pr/SKILL.md`).
```

- [ ] **Step 2: Verify**

Read the file back and confirm the new paragraph reads correctly after step 9's existing content, before the `## Guardrails` section.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/issue-triage/SKILL.md
git commit -m "feat(issue-triage): apply BOT_TRIAGED label after posting the triage comment"
```

---

## Task 2: Test file skeleton and characterisation tests for existing functions

**Files:**
- Create: `tools/test_triage_daemon.py`

**Interfaces:**
- Consumes: `triage_daemon.load_state`, `triage_daemon.save_state`, `triage_daemon.fetch_new_issues` (all pre-existing, unchanged signatures).
- Produces: `DaemonPathsTestCase` — a base `unittest.TestCase` that patches `triage_daemon.BASE_DIR`, `STATE_FILE`, `LOG_DIR`, `CLONE_DIR`, `SCRATCH_DIR` onto a temp directory for the duration of a test, exposing `self.state_file` and `self.log_dir`. Every later task's tests that touch daemon paths or `subprocess.run` inherit from this class.

These are **characterisation tests** for code that already exists and already works — there is no red phase for this task. Write each test, run it, and confirm it passes immediately.

- [ ] **Step 1: Write the test file**

```python
#!/usr/bin/env python3
"""Unit tests for tools/triage_daemon.py.

Runs standalone, not through coverage/run_all: the daemon has no dependency on
apps/predbat, so folding these into TEST_REGISTRY would be a layering violation.
Run directly with `python3 tools/test_triage_daemon.py`. Every gh/git/claude call is
mocked - nothing here touches a real repo, GitHub, or Claude Code session.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import triage_daemon


class DaemonPathsTestCase(unittest.TestCase):
    """Base class that points the daemon's module-level paths at a scratch temp dir."""

    def setUp(self):
        """Create a scratch directory and patch the daemon's path constants onto it."""
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        base = Path(self.tmp_dir.name)
        self.state_file = base / "state.json"
        self.log_dir = base / "logs"
        self._patch("BASE_DIR", base)
        self._patch("STATE_FILE", self.state_file)
        self._patch("LOG_DIR", self.log_dir)
        self._patch("CLONE_DIR", base / "batpred")
        self._patch("SCRATCH_DIR", base / "scratch")

    def _patch(self, name, value):
        """Patch a module-level constant on triage_daemon for the duration of the test."""
        patcher = patch.object(triage_daemon, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)


class LoadSaveStateTests(DaemonPathsTestCase):
    """Characterisation tests for load_state()/save_state() - pre-existing, unchanged."""

    def test_save_then_load_round_trips(self):
        """A saved state dict is returned unchanged by a subsequent load."""
        triage_daemon.save_state({"last_processed": 42})
        self.assertEqual(triage_daemon.load_state(), {"last_processed": 42})

    def test_load_missing_file_returns_default(self):
        """With no state file yet, load_state() returns the zeroed default."""
        self.assertEqual(triage_daemon.load_state(), {"last_processed": 0})

    def test_load_corrupt_file_returns_default(self):
        """A truncated/corrupt state file is treated the same as a missing one."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text("{not valid json")
        self.assertEqual(triage_daemon.load_state(), {"last_processed": 0})


class FetchNewIssuesTests(unittest.TestCase):
    """Characterisation tests for fetch_new_issues() - pre-existing, unchanged."""

    @patch("triage_daemon.subprocess.run")
    def test_filters_and_sorts_by_issue_number(self, mock_run):
        """Only issues newer than since_number are returned, sorted ascending."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps(
                [
                    {"number": 10, "createdAt": "2026-01-01T00:00:00Z"},
                    {"number": 8, "createdAt": "2025-12-01T00:00:00Z"},
                    {"number": 12, "createdAt": "2026-02-01T00:00:00Z"},
                ]
            )
        )
        result = triage_daemon.fetch_new_issues(9)
        self.assertEqual([issue["number"] for issue in result], [10, 12])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and confirm they pass**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: all 4 tests PASS (no red phase — these characterise existing, already-correct behaviour).

- [ ] **Step 3: Commit**

```bash
git add tools/test_triage_daemon.py
git commit -m "test(triage_daemon): add test file with characterisation tests for existing functions"
```

---

## Task 3: `fetch_bot_pr_issues()`

**Files:**
- Modify: `tools/triage_daemon.py` (add function near `fetch_new_issues`)
- Modify: `tools/test_triage_daemon.py`

**Interfaces:**
- Consumes: `REPO` (module constant, pre-existing).
- Produces: `fetch_bot_pr_issues() -> list[dict]`, each dict shaped `{"number": int, "labels": [{"name": str, ...}, ...]}` — Task 9's `process_bot_pr_issue()` iterates this list directly.

- [ ] **Step 1: Write the failing test**

Add to `tools/test_triage_daemon.py`, after `FetchNewIssuesTests`:

```python
class FetchBotPrIssuesTests(unittest.TestCase):
    """Tests for fetch_bot_pr_issues(), new in the bot PR flow."""

    @patch("triage_daemon.subprocess.run")
    def test_queries_open_issues_labelled_bot_pr(self, mock_run):
        """Calls gh issue list scoped to the BOT_PR label and returns the parsed issues."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps([{"number": 4720, "labels": [{"name": "BOT_PR"}, {"name": "BOT_TRIAGED"}]}])
        )
        result = triage_daemon.fetch_bot_pr_issues()
        self.assertEqual(result, [{"number": 4720, "labels": [{"name": "BOT_PR"}, {"name": "BOT_TRIAGED"}]}])
        args = mock_run.call_args[0][0]
        self.assertIn("--label", args)
        self.assertEqual(args[args.index("--label") + 1], "BOT_PR")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: FAIL with `AttributeError: module 'triage_daemon' has no attribute 'fetch_bot_pr_issues'`

- [ ] **Step 3: Implement**

In `tools/triage_daemon.py`, add directly after `fetch_new_issues()`:

```python
def fetch_bot_pr_issues():
    """Return open issues currently labelled BOT_PR, each with its full label list."""
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--state", "open", "--label", "BOT_PR", "--json", "number,labels"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tools/triage_daemon.py tools/test_triage_daemon.py
git commit -m "feat(triage_daemon): add fetch_bot_pr_issues()"
```

---

## Task 4: BOT_PR precondition check (`ensure_triaged` and helpers)

**Files:**
- Modify: `tools/triage_daemon.py`
- Modify: `tools/test_triage_daemon.py`

**Interfaces:**
- Consumes: nothing new (uses stdlib `subprocess`/`json` already imported).
- Produces:
  - `TRIAGE_DISCLOSURE_MARKER` (module constant, str) — the substring identifying a bot triage comment.
  - `is_already_triaged(labels: list[dict]) -> bool`
  - `find_triage_comment(issue_number: int) -> bool`
  - `backfill_triaged_label(issue_number: int) -> None`
  - `ensure_triaged(issue_number: int, labels: list[dict]) -> bool` — Task 9's `process_bot_pr_issue()` calls this directly.

- [ ] **Step 1: Write the failing tests**

Add to `tools/test_triage_daemon.py`, after `FetchBotPrIssuesTests`:

```python
class EnsureTriagedTests(unittest.TestCase):
    """Tests for the BOT_PR precondition check, new in the bot PR flow."""

    def test_is_already_triaged_true_when_label_present(self):
        """BOT_TRIAGED anywhere in the label list is detected."""
        self.assertTrue(triage_daemon.is_already_triaged([{"name": "bug"}, {"name": "BOT_TRIAGED"}]))

    def test_is_already_triaged_false_when_label_absent(self):
        """A label list without BOT_TRIAGED is not mistaken for one."""
        self.assertFalse(triage_daemon.is_already_triaged([{"name": "bug"}]))

    @patch("triage_daemon.subprocess.run")
    def test_find_triage_comment_true_when_disclosure_present(self, mock_run):
        """A comment containing the triage disclosure line counts as already triaged."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"comments": [{"body": "This is an automated first-pass triage of..."}]})
        )
        self.assertTrue(triage_daemon.find_triage_comment(4720))

    @patch("triage_daemon.subprocess.run")
    def test_find_triage_comment_false_when_no_match(self, mock_run):
        """Unrelated comments don't count as a triage comment."""
        mock_run.return_value = MagicMock(stdout=json.dumps({"comments": [{"body": "thanks for the report"}]}))
        self.assertFalse(triage_daemon.find_triage_comment(4720))

    @patch("triage_daemon.subprocess.run")
    def test_ensure_triaged_skips_lookup_when_label_present(self, mock_run):
        """With BOT_TRIAGED already on the issue, no gh calls are made at all."""
        result = triage_daemon.ensure_triaged(4720, [{"name": "BOT_TRIAGED"}])
        self.assertTrue(result)
        mock_run.assert_not_called()

    @patch("triage_daemon.backfill_triaged_label")
    @patch("triage_daemon.find_triage_comment", return_value=True)
    def test_ensure_triaged_backfills_when_comment_found(self, mock_find, mock_backfill):
        """An old issue with a triage comment but no label gets the label backfilled."""
        result = triage_daemon.ensure_triaged(4720, [{"name": "bug"}])
        self.assertTrue(result)
        mock_backfill.assert_called_once_with(4720)

    @patch("triage_daemon.backfill_triaged_label")
    @patch("triage_daemon.find_triage_comment", return_value=False)
    def test_ensure_triaged_false_when_neither_found(self, mock_find, mock_backfill):
        """No label and no comment means /issue-triage still needs to run."""
        result = triage_daemon.ensure_triaged(4720, [{"name": "bug"}])
        self.assertFalse(result)
        mock_backfill.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: FAIL with `AttributeError: module 'triage_daemon' has no attribute 'is_already_triaged'`

- [ ] **Step 3: Implement**

In `tools/triage_daemon.py`, add after `fetch_bot_pr_issues()`:

```python
TRIAGE_DISCLOSURE_MARKER = "automated first-pass triage"


def is_already_triaged(labels):
    """Return True if BOT_TRIAGED is present in a gh --json labels list."""
    return any(label["name"] == "BOT_TRIAGED" for label in labels)


def find_triage_comment(issue_number):
    """Return True if the issue already carries a bot triage comment (pre-BOT_TRIAGED-label issues)."""
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--json", "comments"],
        capture_output=True,
        text=True,
        check=True,
    )
    comments = json.loads(result.stdout).get("comments", [])
    return any(TRIAGE_DISCLOSURE_MARKER in comment.get("body", "") for comment in comments)


def backfill_triaged_label(issue_number):
    """Apply BOT_TRIAGED to an issue found to already have a bot triage comment."""
    subprocess.run(["gh", "issue", "edit", str(issue_number), "--add-label", "BOT_TRIAGED"], check=True)


def ensure_triaged(issue_number, labels):
    """Return True if the issue is (now) marked BOT_TRIAGED; False if /issue-triage still needs to run.

    Checks the label first, then falls back to scanning comments for issues triaged
    before BOT_TRIAGED existed, backfilling the label onto them when found.
    """
    if is_already_triaged(labels):
        return True
    if find_triage_comment(issue_number):
        backfill_triaged_label(issue_number)
        return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: PASS (11 tests total so far)

- [ ] **Step 5: Commit**

```bash
git add tools/triage_daemon.py tools/test_triage_daemon.py
git commit -m "feat(triage_daemon): add BOT_PR precondition check (ensure_triaged)"
```

---

## Task 5: Duplicate-work guard

**Files:**
- Modify: `tools/triage_daemon.py`
- Modify: `tools/test_triage_daemon.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `build_duplicate_search_query(issue_number: int) -> str`, `has_existing_pr(issue_number: int) -> bool` — Task 9's `process_bot_pr_issue()` calls `has_existing_pr()` both as the entry guard and as the post-hoc success check.

- [ ] **Step 1: Write the failing tests**

Add to `tools/test_triage_daemon.py`, after `EnsureTriagedTests`:

```python
class DuplicateGuardTests(unittest.TestCase):
    """Tests for the duplicate-work guard, new in the bot PR flow."""

    def test_search_query_uses_quoted_fixes_phrase(self):
        """The query searches the exact quoted phrase, not a bare issue number."""
        self.assertEqual(triage_daemon.build_duplicate_search_query(4720), '"Fixes #4720" in:body')

    @patch("triage_daemon.subprocess.run")
    def test_has_existing_pr_true_when_match_found(self, mock_run):
        """A non-empty search result means a PR already exists for this issue."""
        mock_run.return_value = MagicMock(stdout=json.dumps([{"number": 99}]))
        self.assertTrue(triage_daemon.has_existing_pr(4720))

    @patch("triage_daemon.subprocess.run")
    def test_has_existing_pr_false_when_no_match(self, mock_run):
        """An empty search result means no PR exists yet for this issue."""
        mock_run.return_value = MagicMock(stdout=json.dumps([]))
        self.assertFalse(triage_daemon.has_existing_pr(4720))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: FAIL with `AttributeError: module 'triage_daemon' has no attribute 'build_duplicate_search_query'`

- [ ] **Step 3: Implement**

In `tools/triage_daemon.py`, add after `ensure_triaged()`:

```python
def build_duplicate_search_query(issue_number):
    """Build the gh pr list --search query used to detect an existing PR for an issue.

    Matches the exact "Fixes #N" phrase the issue-pr skill always includes in its PR
    body, rather than a bare issue number, which could match an unrelated PR.
    """
    return f'"Fixes #{issue_number}" in:body'


def has_existing_pr(issue_number):
    """Return True if a PR already references this issue, in any state."""
    query = build_duplicate_search_query(issue_number)
    result = subprocess.run(
        ["gh", "pr", "list", "--search", query, "--state", "all", "--json", "number"],
        capture_output=True,
        text=True,
        check=True,
    )
    return len(json.loads(result.stdout)) > 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: PASS (14 tests total so far)

- [ ] **Step 5: Commit**

```bash
git add tools/triage_daemon.py tools/test_triage_daemon.py
git commit -m "feat(triage_daemon): add duplicate-work guard (has_existing_pr)"
```

---

## Task 6: Label-swap helpers

**Files:**
- Modify: `tools/triage_daemon.py`
- Modify: `tools/test_triage_daemon.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `mark_pr_opened(issue_number: int) -> None`, `mark_pr_failed(issue_number: int) -> None` — Task 9's `process_bot_pr_issue()` calls exactly one of these per run.

- [ ] **Step 1: Write the failing tests**

Add to `tools/test_triage_daemon.py`, after `DuplicateGuardTests`:

```python
class LabelSwapTests(unittest.TestCase):
    """Tests for mark_pr_opened/mark_pr_failed, new in the bot PR flow."""

    @patch("triage_daemon.subprocess.run")
    def test_mark_pr_opened_swaps_labels(self, mock_run):
        """Removes BOT_PR and adds BOT_PR_OPENED."""
        triage_daemon.mark_pr_opened(4720)
        args = mock_run.call_args[0][0]
        self.assertEqual(args, ["gh", "issue", "edit", "4720", "--remove-label", "BOT_PR", "--add-label", "BOT_PR_OPENED"])

    @patch("triage_daemon.subprocess.run")
    def test_mark_pr_failed_swaps_labels(self, mock_run):
        """Removes BOT_PR and adds BOT_PR_FAILED."""
        triage_daemon.mark_pr_failed(4720)
        args = mock_run.call_args[0][0]
        self.assertEqual(args, ["gh", "issue", "edit", "4720", "--remove-label", "BOT_PR", "--add-label", "BOT_PR_FAILED"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: FAIL with `AttributeError: module 'triage_daemon' has no attribute 'mark_pr_opened'`

- [ ] **Step 3: Implement**

In `tools/triage_daemon.py`, add after `has_existing_pr()`:

```python
def mark_pr_opened(issue_number):
    """Swap BOT_PR for BOT_PR_OPENED once the draft PR has been confirmed open."""
    subprocess.run(
        ["gh", "issue", "edit", str(issue_number), "--remove-label", "BOT_PR", "--add-label", "BOT_PR_OPENED"],
        check=True,
    )


def mark_pr_failed(issue_number):
    """Swap BOT_PR for BOT_PR_FAILED so a failed run isn't retried every poll cycle."""
    subprocess.run(
        ["gh", "issue", "edit", str(issue_number), "--remove-label", "BOT_PR", "--add-label", "BOT_PR_FAILED"],
        check=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: PASS (16 tests total so far)

- [ ] **Step 5: Commit**

```bash
git add tools/triage_daemon.py tools/test_triage_daemon.py
git commit -m "feat(triage_daemon): add mark_pr_opened/mark_pr_failed label-swap helpers"
```

---

## Task 7: Permission model — `ALLOWED_TOOLS_PR` / `DISALLOWED_TOOLS_PR`

**Files:**
- Modify: `tools/triage_daemon.py:83-174` (the existing `ALLOWED_TOOLS`/`DISALLOWED_TOOLS` block)
- Modify: `tools/test_triage_daemon.py`

**Interfaces:**
- Consumes: `EDIT_SCOPE`, `SCRATCH_SCOPE` (pre-existing module constants, unchanged).
- Produces: `ALLOWED_TOOLS` and `DISALLOWED_TOOLS` (pre-existing names, values byte-identical to before this task), plus new `ALLOWED_TOOLS_PR` and `DISALLOWED_TOOLS_PR` (both comma-joined strings, same format `triage()`/Task 8's `create_pr()` already expect from `ALLOWED_TOOLS`/`DISALLOWED_TOOLS`).

This task is a **refactor with no behaviour change** to `ALLOWED_TOOLS`/`DISALLOWED_TOOLS` — it extracts their list literals into named base lists so the PR variants can be derived without duplicating ~50 lines of text. The tests exist specifically to pin that relationship down, since this is the security boundary between "opens a draft PR" and "can merge/close/administer the repo."

- [ ] **Step 1: Write the failing tests**

Add to `tools/test_triage_daemon.py`, after `LabelSwapTests`:

```python
class PermissionModelTests(unittest.TestCase):
    """Regression tests for the ALLOWED_TOOLS/DISALLOWED_TOOLS delta between the
    read-only triage invocation and the push/PR-create-capable /issue-pr invocation.
    This boundary should never drift silently."""

    def test_triage_allowed_tools_still_contains_the_broad_gh_grant(self):
        """Sanity check that the refactor didn't drop the base allowlist's core entry."""
        self.assertIn("Bash(gh *)", triage_daemon.ALLOWED_TOOLS.split(","))

    def test_pr_disallowed_tools_still_blocks_dangerous_gh_subcommands(self):
        """Everything dangerous stays denied for the PR flow too."""
        still_denied = [
            "Bash(gh pr merge*)",
            "Bash(gh pr close*)",
            "Bash(gh repo*)",
            "Bash(gh release*)",
            "Bash(gh workflow*)",
            "Bash(gh auth*)",
            "Bash(gh secret*)",
            "Bash(gh api*)",
            "mcp__*",
        ]
        pr_denied = triage_daemon.DISALLOWED_TOOLS_PR.split(",")
        for entry in still_denied:
            self.assertIn(entry, pr_denied)

    def test_pr_disallowed_tools_carves_out_exactly_three_entries(self):
        """Only git push, git commit and gh pr create are removed from the denial list."""
        base = set(triage_daemon.DISALLOWED_TOOLS.split(","))
        pr = set(triage_daemon.DISALLOWED_TOOLS_PR.split(","))
        self.assertEqual(base - pr, {"Bash(git push*)", "Bash(git commit*)", "Bash(gh pr create*)"})

    def test_pr_allowed_tools_is_a_superset_of_the_triage_allowlist(self):
        """The PR flow's allowlist only ever adds to the read-only allowlist, never removes."""
        base = set(triage_daemon.ALLOWED_TOOLS.split(","))
        pr = set(triage_daemon.ALLOWED_TOOLS_PR.split(","))
        self.assertTrue(base.issubset(pr))

    def test_pr_allowed_tools_adds_exactly_the_expected_entries(self):
        """The only additions are add/commit/push/pr-create/pre-commit."""
        base = set(triage_daemon.ALLOWED_TOOLS.split(","))
        pr = set(triage_daemon.ALLOWED_TOOLS_PR.split(","))
        self.assertEqual(
            pr - base,
            {
                "Bash(git add*)",
                "Bash(git commit*)",
                "Bash(git push*)",
                "Bash(gh pr create*)",
                "Bash(./run_pre_commit*)",
                "Bash(./run_pre_commit)",
            },
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: FAIL with `AttributeError: module 'triage_daemon' has no attribute 'ALLOWED_TOOLS_PR'` (the first test passes; the rest fail on the missing attribute).

- [ ] **Step 3: Implement**

In `tools/triage_daemon.py`, replace the entire existing block from `EDIT_SCOPE = ...` through the end of the `DISALLOWED_TOOLS = ",".join(...)` call (currently lines 83-174) with:

```python
EDIT_SCOPE = f"//{CLONE_DIR.relative_to('/')}/**"
SCRATCH_SCOPE = f"//{SCRATCH_DIR.relative_to('/')}/**"
_ALLOWED_TOOLS_BASE = [
    # Read the issue, search for duplicates, post the triage comment
    "Bash(gh *)",
    # Git history, and the re-sync/discard the skill does before investigating
    "Bash(git log*)",
    "Bash(git diff*)",
    "Bash(git show*)",
    "Bash(git blame*)",
    "Bash(git grep*)",
    "Bash(git status*)",
    "Bash(git branch*)",
    "Bash(git tag*)",
    "Bash(git describe*)",
    "Bash(git rev-parse*)",
    "Bash(git remote -v*)",
    "Bash(git remote show*)",
    "Bash(git fetch*)",
    "Bash(git reset*)",
    "Bash(git checkout*)",
    "Bash(git restore*)",
    "Bash(git clean*)",
    "Bash(git stash*)",
    # Running one targeted test. tools/triage_test.sh keeps the cd into
    # coverage/ and the output redirect inside the script, because Claude Code
    # prompts on a cd combined with an output redirect - which is what the
    # obvious "cd coverage && ./run_all --test X > log 2>&1" form is, so it is
    # denied outright under dontAsk no matter what rules are listed here.
    "Bash(tools/triage_test.sh *)",
    "Bash(./tools/triage_test.sh *)",
    "Bash(cd *)",
    "Bash(./run_all *)",
    "Bash(./run_all)",
    "Bash(python3 *)",
    # Pulling issue attachments down and picking them apart. A predbat.log
    # is often tens of MB - too big for WebFetch and too big to read into
    # context, so it gets downloaded and grepped instead.
    "Bash(curl *)",
    "Bash(mkdir *)",
    "Bash(unzip *)",
    "Bash(gunzip *)",
    "Bash(zcat *)",
    "Bash(tar *)",
    "Bash(file *)",
    "Bash(ls *)",
    "Bash(find *)",
    "Bash(wc *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(cat *)",
    "Bash(grep *)",
    "Bash(rg *)",
    "Bash(sed *)",
    "Bash(awk *)",
    "Bash(sort *)",
    "Bash(uniq *)",
    "Bash(cut *)",
    "Bash(tr *)",
    "Bash(tee *)",
    "Bash(echo *)",
    # Edit rules cover every file-editing tool (Write included) - a Write(path)
    # rule is not matched by the file permission check, so don't add one.
    f"Edit({EDIT_SCOPE})",
    f"Edit({SCRATCH_SCOPE})",
    "WebFetch",
    "Read",
    "Grep",
    "Glob",
]
ALLOWED_TOOLS = ",".join(_ALLOWED_TOOLS_BASE)
# The /issue-pr invocation needs everything the read-only triage flow has, plus
# committing/pushing its branch, opening the PR, and running pre-commit as a quality gate.
_ALLOWED_TOOLS_PR_EXTRA = [
    "Bash(git add*)",
    "Bash(git commit*)",
    "Bash(git push*)",
    "Bash(gh pr create*)",
    "Bash(./run_pre_commit*)",
    "Bash(./run_pre_commit)",
]
ALLOWED_TOOLS_PR = ",".join(_ALLOWED_TOOLS_BASE + _ALLOWED_TOOLS_PR_EXTRA)
# Deny wins over allow, so these carve the publishing commands back out of
# the broad "Bash(gh *)" / "Bash(git ...)" entries above.
_DISALLOWED_TOOLS_BASE = [
    "mcp__*",
    "Bash(git push*)",
    "Bash(git commit*)",
    "Bash(git remote add*)",
    "Bash(git remote set-url*)",
    "Bash(gh pr create*)",
    "Bash(gh pr merge*)",
    "Bash(gh pr close*)",
    "Bash(gh repo*)",
    "Bash(gh release*)",
    "Bash(gh workflow*)",
    "Bash(gh auth*)",
    "Bash(gh secret*)",
    "Bash(gh api*)",
]
DISALLOWED_TOOLS = ",".join(_DISALLOWED_TOOLS_BASE)
# The PR flow needs to push its branch and open the PR - carve those three back out of
# the base denial list. Everything else (merge, close, repo/release/workflow/auth/secret/
# api admin, all mcp__*) stays denied.
_PR_REMOVED_DENIALS = {"Bash(git push*)", "Bash(git commit*)", "Bash(gh pr create*)"}
DISALLOWED_TOOLS_PR = ",".join(item for item in _DISALLOWED_TOOLS_BASE if item not in _PR_REMOVED_DENIALS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: PASS (21 tests total so far)

- [ ] **Step 5: Commit**

```bash
git add tools/triage_daemon.py tools/test_triage_daemon.py
git commit -m "refactor(triage_daemon): derive ALLOWED_TOOLS_PR/DISALLOWED_TOOLS_PR from the triage allowlist"
```

---

## Task 8: `create_pr()`

**Files:**
- Modify: `tools/triage_daemon.py`
- Modify: `tools/test_triage_daemon.py`

**Interfaces:**
- Consumes: `ALLOWED_TOOLS_PR`, `DISALLOWED_TOOLS_PR` (Task 7), `CLONE_DIR`, `SCRATCH_DIR`, `LOG_DIR` (pre-existing module constants).
- Produces: `create_pr(issue_number: int) -> None` — Task 9's `process_bot_pr_issue()` calls this and then checks `has_existing_pr()` afterwards; `create_pr()`'s own return value carries no success/failure meaning (see docstring — a `claude -p` session exits 0 whether it opened a PR or posted a failure comment instead, so the exit code can't be used to decide the outcome).

- [ ] **Step 1: Write the failing tests**

Add to `tools/test_triage_daemon.py`, after `PermissionModelTests`:

```python
class CreatePrTests(DaemonPathsTestCase):
    """Tests for create_pr(), new in the bot PR flow."""

    @patch("triage_daemon.subprocess.run")
    def test_invokes_claude_with_the_pr_permission_set(self, mock_run):
        """Runs the /issue-pr skill with the PR-flow allow/deny lists and budget caps."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.create_pr(4720)
        cmd = mock_run.call_args[0][0]
        self.assertIn("/issue-pr 4720", cmd[2])
        self.assertIn(triage_daemon.ALLOWED_TOOLS_PR, cmd)
        self.assertIn(triage_daemon.DISALLOWED_TOOLS_PR, cmd)
        self.assertIn("150", cmd)
        self.assertIn("25.00", cmd)

    @patch("triage_daemon.subprocess.run")
    def test_does_not_raise_on_a_non_zero_exit(self, mock_run):
        """Unlike triage(), a non-zero exit is logged, not raised - the caller decides
        success by checking for the PR afterwards, not from this function's return."""
        mock_run.return_value = MagicMock(returncode=1)
        triage_daemon.create_pr(4720)  # must not raise

    @patch("triage_daemon.subprocess.run")
    def test_writes_a_log_file(self, mock_run):
        """A per-issue PR-flow log file is created under LOG_DIR."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.create_pr(4720)
        self.assertTrue((self.log_dir / "issue-4720-pr.log").exists())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: FAIL with `AttributeError: module 'triage_daemon' has no attribute 'create_pr'`

- [ ] **Step 3: Implement**

In `tools/triage_daemon.py`, add directly after `triage()`:

```python
def create_pr(issue_number):
    """Run the /issue-pr skill for one issue under the push/PR-create-capable permission set.

    Whether it succeeded is judged by the caller re-checking has_existing_pr()
    afterwards, not by this function's return value or the invocation's exit code -
    a claude -p session that completes normally exits 0 whether it opened a PR or
    decided the quality gate failed and posted a comment instead.
    """
    cmd = [
        "claude",
        "-p",
        f"/issue-pr {issue_number} scratch={SCRATCH_DIR}",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        ALLOWED_TOOLS_PR,
        "--disallowedTools",
        DISALLOWED_TOOLS_PR,
        "--verbose",
        "--add-dir",
        str(SCRATCH_DIR),
        "--max-turns",
        "150",
        "--max-budget-usd",
        "25.00",
    ]
    log_path = LOG_DIR / f"issue-{issue_number}-pr.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[pr] issue #{issue_number}: starting, logging to {log_path}", flush=True)
    with log_path.open("a") as log_handle:
        log_handle.write(f"\n==== issue #{issue_number} PR flow started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
        log_handle.flush()
        result = subprocess.run(cmd, cwd=str(CLONE_DIR), stdout=log_handle, stderr=subprocess.STDOUT)
        log_handle.write(f"==== issue #{issue_number} PR flow exited {result.returncode} ====\n")
    print(f"[pr] issue #{issue_number}: exited {result.returncode}", flush=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: PASS (24 tests total so far)

- [ ] **Step 5: Commit**

```bash
git add tools/triage_daemon.py tools/test_triage_daemon.py
git commit -m "feat(triage_daemon): add create_pr() to invoke the /issue-pr skill"
```

---

## Task 9: `process_bot_pr_issue()` and daemon loop wiring

**Files:**
- Modify: `tools/triage_daemon.py` (`main()`)
- Modify: `tools/test_triage_daemon.py`

**Interfaces:**
- Consumes: `has_existing_pr` (Task 5), `sync_repo`, `reset_scratch`, `triage` (all pre-existing), `ensure_triaged` (Task 4), `create_pr` (Task 8), `mark_pr_opened`, `mark_pr_failed` (Task 6).
- Produces: `process_bot_pr_issue(issue: dict) -> None`, called from `main()` for every result of `fetch_bot_pr_issues()`.

- [ ] **Step 1: Write the failing tests**

Add to `tools/test_triage_daemon.py`, after `CreatePrTests`:

```python
class ProcessBotPrIssueTests(unittest.TestCase):
    """Tests for process_bot_pr_issue(), the orchestrator wiring the whole BOT_PR flow
    together - new in the bot PR flow."""

    def setUp(self):
        """Patch every collaborator process_bot_pr_issue() calls."""
        self.patches = {}
        for name in [
            "has_existing_pr",
            "sync_repo",
            "reset_scratch",
            "ensure_triaged",
            "triage",
            "create_pr",
            "mark_pr_opened",
            "mark_pr_failed",
        ]:
            patcher = patch.object(triage_daemon, name)
            self.patches[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def test_skips_entirely_when_pr_already_exists(self):
        """An issue that already has a referencing PR is left alone."""
        self.patches["has_existing_pr"].return_value = True
        triage_daemon.process_bot_pr_issue({"number": 4720, "labels": []})
        self.patches["sync_repo"].assert_not_called()
        self.patches["create_pr"].assert_not_called()

    def test_runs_triage_first_when_not_yet_triaged(self):
        """An untriaged issue is triaged before the PR flow runs."""
        self.patches["has_existing_pr"].side_effect = [False, True]  # entry guard, then post-hoc check
        self.patches["ensure_triaged"].return_value = False
        triage_daemon.process_bot_pr_issue({"number": 4720, "labels": []})
        self.patches["triage"].assert_called_once_with(4720)
        self.patches["create_pr"].assert_called_once_with(4720)

    def test_skips_triage_when_already_triaged(self):
        """An already-triaged issue goes straight to the PR flow."""
        self.patches["has_existing_pr"].side_effect = [False, True]
        self.patches["ensure_triaged"].return_value = True
        triage_daemon.process_bot_pr_issue({"number": 4720, "labels": [{"name": "BOT_TRIAGED"}]})
        self.patches["triage"].assert_not_called()
        self.patches["create_pr"].assert_called_once_with(4720)

    def test_marks_opened_when_a_pr_exists_afterwards(self):
        """If a PR references the issue after create_pr() runs, swap to BOT_PR_OPENED."""
        self.patches["has_existing_pr"].side_effect = [False, True]
        self.patches["ensure_triaged"].return_value = True
        triage_daemon.process_bot_pr_issue({"number": 4720, "labels": []})
        self.patches["mark_pr_opened"].assert_called_once_with(4720)
        self.patches["mark_pr_failed"].assert_not_called()

    def test_marks_failed_when_no_pr_exists_afterwards(self):
        """If no PR exists after create_pr() runs (quality gate failed), swap to BOT_PR_FAILED."""
        self.patches["has_existing_pr"].side_effect = [False, False]
        self.patches["ensure_triaged"].return_value = True
        triage_daemon.process_bot_pr_issue({"number": 4720, "labels": []})
        self.patches["mark_pr_failed"].assert_called_once_with(4720)
        self.patches["mark_pr_opened"].assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: FAIL with `AttributeError: module 'triage_daemon' has no attribute 'process_bot_pr_issue'`

- [ ] **Step 3: Implement**

In `tools/triage_daemon.py`, add directly after `create_pr()`:

```python
def process_bot_pr_issue(issue):
    """Run the full BOT_PR flow for one issue: guard against duplicate work, ensure it's
    triaged, run the PR flow, then swap the label based on whether a PR now exists.
    """
    issue_number = issue["number"]
    labels = issue.get("labels", [])
    if has_existing_pr(issue_number):
        print(f"[pr] issue #{issue_number}: a PR already references this issue, skipping", flush=True)
        return
    sync_repo()
    reset_scratch()
    if not ensure_triaged(issue_number, labels):
        triage(issue_number)
    create_pr(issue_number)
    if has_existing_pr(issue_number):
        mark_pr_opened(issue_number)
    else:
        mark_pr_failed(issue_number)
```

Then modify `main()` to also process `BOT_PR` issues each poll cycle. Find:

```python
    state = load_state()
    while True:
        try:
            for issue in fetch_new_issues(state["last_processed"]):
                sync_repo()
                reset_scratch()
                triage(issue["number"])
                state["last_processed"] = issue["number"]
                save_state(state)
        except subprocess.CalledProcessError as exc:
            print(f"[triage] error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)
```

Replace it with:

```python
    state = load_state()
    while True:
        try:
            for issue in fetch_new_issues(state["last_processed"]):
                sync_repo()
                reset_scratch()
                triage(issue["number"])
                state["last_processed"] = issue["number"]
                save_state(state)
            for issue in fetch_bot_pr_issues():
                process_bot_pr_issue(issue)
        except subprocess.CalledProcessError as exc:
            print(f"[triage] error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 tools/test_triage_daemon.py -v`
Expected: PASS (29 tests total)

- [ ] **Step 5: Commit**

```bash
git add tools/triage_daemon.py tools/test_triage_daemon.py
git commit -m "feat(triage_daemon): add process_bot_pr_issue() and wire it into the poll loop"
```

---

## Task 10: New skill — `.claude/skills/issue-pr/SKILL.md`

**Files:**
- Create: `.claude/skills/issue-pr/SKILL.md`

**Interfaces:**
- Consumes: `.claude/skills/issue-triage/references/debug-journal.md` (existing, referenced not duplicated), `CLAUDE.md` (repo root, read automatically by Claude Code from the working directory).
- Produces: the `/issue-pr <issue-number> [scratch=<dir>]` slash command Task 8's `create_pr()` invokes.

- [ ] **Step 1: Write the skill file**

```markdown
---
name: issue-pr
description: Implement the fix or feature described in an already-triaged batpred GitHub issue and open a draft pull request referencing it, for maintainer review.
allowed-tools: You can view and edit files in this repo, write code and tests, run local tests and pre-commit, commit and push a new branch, and open a draft pull request with 'gh'. Do not merge, close, or push directly to main.
---

# Issue PR

You are implementing the fix or feature described in one already-triaged GitHub issue on `springfall2008/batpred`, and opening a draft pull request for maintainer review. The working directory is the same dedicated local clone the triage skill uses — investigate and work using it directly.

Arguments: `<issue-number> [scratch=<dir>]`, same convention as `/issue-triage`. If no `scratch=` is given (an interactive invocation), use `/tmp/predbat-triage/<issue-number>` and `mkdir -p` it yourself.

This skill assumes the issue has already been triaged — the daemon only invokes it once that's confirmed. Read the existing triage comment before doing anything else.

## 1. Read the ticket

Fetch it with `gh issue view <number> --json title,body,labels,comments`. The bot's own triage comment (opens with "automated first-pass triage") already has the classification, priority, and any root-cause pointer — start from there rather than re-investigating from scratch.

## 2. Investigate

- Sync the clone first, so you are reading the code you think you are:

  ```bash
  git fetch origin main && git reset --hard origin/main && git clean -fd
  git describe --tags
  ```

- Read [../issue-triage/references/debug-journal.md](../issue-triage/references/debug-journal.md) before forming an implementation approach — it maps common symptoms to modules and records known per-integration behaviour from past investigations. Its entries are dated observations, not current truth — confirm anything you rely on against the working tree.
- Read the relevant source area named in the triage comment's root-cause pointer, or that the issue's classification points to.
- Check `git log`/`git blame` on that area — the triage comment may already cover this, but confirm nothing has changed on `main` since triage ran.

## 3. Implement

- Write the fix or feature following this repo's conventions (`CLAUDE.md`, already loaded automatically for this session): `lower_case_with_underscores` naming, 256-character line length, a one-line docstring on every new function or class.
- Add or update a unit test for the change, in the matching `apps/predbat/tests/test_<feature>.py` module (registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py` if it's a new module) — `CLAUDE.md` requires unit tests for all new code, no exceptions for bot-authored ones.
- Keep the change scoped to what the issue describes. Don't refactor unrelated code, even if you notice something else worth fixing.

## 4. Quality gate

Both of these must pass before you continue to step 5:

```bash
./run_pre_commit
tools/triage_test.sh <name> <scratch>/test.log
```

Use the test module named in the triage comment, or the one `TEST_REGISTRY` maps to the area you changed. If there's no clean single-module mapping, run `./run_all --quick` instead (from `coverage/`) rather than guessing at a module name.

If either fails, stop here — do not commit, push, or open a PR. Go to step 7 and report what failed.

## 5. Branch, commit, push

Branch name: `fix/<slug>-<issue-number>` for a `bug` classification, `feat/<slug>-<issue-number>` for `enhancement` — matching this repo's existing convention (e.g. `fix/solis-tou-bit-refused-4707`). `<slug>` is a short kebab-case description of the change.

```bash
git checkout -b fix/<slug>-<issue-number>
git add <changed files>
git commit -m "<one-line summary of the fix>"
git push -u origin fix/<slug>-<issue-number>
```

Never push to `main`, and never force-push.

## 6. Open the draft PR

```bash
gh pr create --draft --assignee springfall2008 \
  --title "<one-line summary>" \
  --body "$(cat <<'EOF'
This is an automated draft PR generated from issue #<issue-number> — a maintainer should review it before merging.

Fixes #<issue-number>

## Summary

<what changed and why, in a sentence or two>

## Testing

<what you ran in step 4 and its result>

## Notes

<a debug-journal.md reference if one informed the approach, otherwise omit this section>
EOF
)"
```

This is the last step on success — there is nothing further to report; the daemon detects the new PR itself.

## 7. On failure

If step 4's quality gate failed, or an earlier step couldn't proceed (e.g. the fix genuinely needs information only a maintainer has), post exactly one comment on the issue via `gh issue comment <number> --body "..."` explaining plainly what you attempted and what failed — never word it so a skipped step reads as one you completed (same guardrail as the triage skill). Then stop. Do not commit, push, or open a PR — the daemon detects this outcome itself by finding no PR referencing the issue afterwards.

## Guardrails

- Never push to `main` or force-push.
- Never merge, close, or edit an existing PR.
- Never remove a label a human applied.
- The PR is always a draft — never open it as ready-for-review.
- If a command you needed was blocked by permissions, say plainly in the failure comment what you could not do.
```

- [ ] **Step 2: Verify**

Read the file back and confirm the frontmatter parses (matches the shape of `.claude/skills/issue-triage/SKILL.md`'s frontmatter) and the relative link to `debug-journal.md` resolves: `ls .claude/skills/issue-triage/references/debug-journal.md` from repo root.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/issue-pr/SKILL.md
git commit -m "feat(issue-pr): add the issue-pr skill"
```

---

## Task 11: Wire the new tests into pre-commit

**Files:**
- Modify: `.pre-commit-config.yaml`

**Interfaces:**
- Consumes: `tools/test_triage_daemon.py` (Tasks 2–9).
- Produces: nothing consumed by later tasks — this is the last automated task.

- [ ] **Step 1: Add the local hook**

In `.pre-commit-config.yaml`, add a new hook to the existing `repo: local` block (the one that already contains `cspell-dictionary-sorter`):

Find:

```yaml
- repo: local
  hooks:
  - id: cspell-dictionary-sorter
    name: cspell dictionary sorter
    language: python
    entry: python .cspell/sort_dictionary.py
    files: ^\.cspell\/custom-dictionary-workspace\.txt$
```

Replace with:

```yaml
- repo: local
  hooks:
  - id: cspell-dictionary-sorter
    name: cspell dictionary sorter
    language: python
    entry: python .cspell/sort_dictionary.py
    files: ^\.cspell\/custom-dictionary-workspace\.txt$
  - id: triage-daemon-tests
    name: triage daemon unit tests
    language: python
    entry: python tools/test_triage_daemon.py
    files: ^tools/(triage_daemon\.py|test_triage_daemon\.py)$
    pass_filenames: false
```

- [ ] **Step 2: Verify the hook runs and passes**

Run: `cd coverage && source setup.csh && python3 -m pre_commit run triage-daemon-tests --files ../tools/triage_daemon.py`
Expected: the hook fires and reports success (29 tests passing).

- [ ] **Step 3: Commit**

```bash
git add .pre-commit-config.yaml
git commit -m "chore: run triage_daemon.py unit tests via pre-commit"
```

---

## Task 12: Create the GitHub labels — **manual, run by the user, not automated**

This task modifies live state on `springfall2008/batpred` (visible labels on a public repo). Do not run these commands as part of automated task execution — present them to the user and let them run it, or get explicit confirmation before running.

- [ ] **Step 1: Create the four labels**

```bash
gh label create BOT_TRIAGED --repo springfall2008/batpred --color 0e8a16 --description "Has been through the triage bot"
gh label create BOT_PR --repo springfall2008/batpred --color 5319e7 --description "Trigger: bot should implement this ticket and open a draft PR"
gh label create BOT_PR_OPENED --repo springfall2008/batpred --color 0e8a16 --description "Bot PR flow opened a draft PR for this ticket"
gh label create BOT_PR_FAILED --repo springfall2008/batpred --color d93f0b --description "Bot PR flow's quality gate failed; see the comment for details"
```

- [ ] **Step 2: Verify**

```bash
gh label list --repo springfall2008/batpred | grep -E "^BOT_"
```

Expected: all four labels listed.

- [ ] **Step 3: Confirm the daemon's `gh auth` identity has push access**

Per the spec (Section 6.1): whatever GitHub identity `tools/triage_daemon.py` runs as needs push access to `springfall2008/batpred`. No separate bot account is required — the PR uses `--assignee springfall2008` rather than `--reviewer`, and self-assignment is allowed, so the daemon can run as `springfall2008` itself. Confirm push access on whichever machine runs the daemon before enabling `BOT_PR` for real use:

```bash
gh auth status
```

---

## Task 13: End-to-end verification — **manual, human-supervised, not automated**

This exercises the live daemon against the real repo with push/PR-create permissions — do not run unsupervised. Requires Task 12 complete.

- [ ] **Step 1: Dry run against a real triaged issue**

Pick an issue already carrying a bot triage comment (or let the daemon triage a fresh one first). Add the `BOT_PR` label:

```bash
gh issue edit <number> --repo springfall2008/batpred --add-label BOT_PR
```

- [ ] **Step 2: Watch the daemon log**

```bash
tail -f ~/predbat-triage-bot/logs/issue-<number>-pr.log
```

Confirm: the branch is created, the commit looks right, `./run_pre_commit` and the targeted test both pass in the log, the branch is pushed, and a draft PR appears on GitHub with `springfall2008` as assignee, `Fixes #<number>` in the body, and the disclosure line.

- [ ] **Step 3: Confirm the label swap**

```bash
gh issue view <number> --repo springfall2008/batpred --json labels
```

Expected: `BOT_PR` is gone, `BOT_PR_OPENED` is present.

- [ ] **Step 4: Confirm the failure path separately**

Add `BOT_PR` to a ticket engineered to fail the quality gate (e.g. one whose obvious fix would break an existing test). Confirm: no branch is pushed, no PR opens, one comment is posted explaining the failure, and the issue ends up with `BOT_PR_FAILED` instead of `BOT_PR`.

---

## Self-Review Notes

- **Spec coverage:** Section 2 (labels) → Tasks 1, 12. Section 2.1 (backfill) → Task 4. Section 3 (issue-triage modification) → Task 1. Section 4 (daemon changes, including the corrected success-detection mechanism) → Tasks 3, 5, 6, 9. Section 5 (issue-pr skill) → Task 10. Section 6 (permission model) → Tasks 7, 8; Section 6.1 (infra prerequisite) → Task 12 Step 3. Section 7.1 (unit tests) → Tasks 2–9. Section 7.2 (pre-commit wiring) → Task 11. Section 7.3 (E2E verification) → Task 13. Section 8 (error handling table) → covered across Tasks 4, 5, 9.
- **Placeholder scan:** no TBD/TODO; every code block is complete, runnable code, not a description of code.
- **Type consistency:** `has_existing_pr(issue_number)`, `ensure_triaged(issue_number, labels)`, `create_pr(issue_number)`, `mark_pr_opened(issue_number)`/`mark_pr_failed(issue_number)` use the same parameter names and call signatures everywhere they appear across Tasks 4–9. `process_bot_pr_issue(issue)` consistently takes the raw dict shape `fetch_bot_pr_issues()` (Task 3) produces.
