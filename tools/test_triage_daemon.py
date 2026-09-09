#!/usr/bin/env python3
"""Unit tests for tools/triage_daemon.py.

Runs standalone, not through coverage/run_all: the daemon has no dependency on
apps/predbat, so folding these into TEST_REGISTRY would be a layering violation.
Run directly with `python3 tools/test_triage_daemon.py`. Every gh/git/claude call is
mocked - nothing here touches a real repo, GitHub, or Claude Code session.
"""

import fnmatch
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import triage_daemon


# What `gh pr list --json number` prints once the flush has opened its PR. The flush reads
# this to decide whether the queue may be consumed, so the tests need both shapes.
OPENED_PR_JSON = '[{"number": 4980}]'


def bash_rule_matches(rule, command):
    """Simulate Claude Code's Bash(...) permission-rule prefix-glob matching against a command."""
    pattern = rule.removeprefix("Bash(").removesuffix(")")
    return fnmatch.fnmatchcase(command, pattern)


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
        self._patch("QUEUE_DIR", base / "journal-queue")
        # Derived from a patched directory at import time, so patching the directory alone
        # leaves these pointing at the operator's live bot directory. Any new
        # PATH = <patched dir> / ... constant needs adding here too.
        self._patch("GITNEXUS_RUNNER", base / "batpred" / ".gitnexus" / "run.cjs")
        self._patch("GITNEXUS_HEAD_FILE", base / "gitnexus-head")
        self._patch("MCP_CONFIG_FILE", base / "mcp-gitnexus.json")

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


class IssueUrlTests(unittest.TestCase):
    """Tests for issue_url(), new - used to print an openable link when work starts."""

    def test_builds_the_github_issue_url(self):
        """Returns the standard GitHub issue URL for the configured repo."""
        self.assertEqual(triage_daemon.issue_url(4720), "https://github.com/springfall2008/batpred/issues/4720")


class PrUrlTests(unittest.TestCase):
    """Tests for pr_url(), new - used to print an openable link when work starts."""

    def test_builds_the_github_pr_url(self):
        """Returns the standard GitHub PR URL for the configured repo."""
        self.assertEqual(triage_daemon.pr_url(4720), "https://github.com/springfall2008/batpred/pull/4720")


class FetchNewIssuesTests(unittest.TestCase):
    """Characterisation tests for fetch_new_issues() - pre-existing, except for also
    requesting the title field (used to print an openable link when work starts)."""

    @patch("triage_daemon.subprocess.run")
    def test_filters_and_sorts_by_issue_number(self, mock_run):
        """Only issues newer than since_number are returned, sorted ascending."""
        mock_run.return_value = MagicMock(
            stdout=json.dumps(
                [
                    {"number": 10, "createdAt": "2026-01-01T00:00:00Z", "title": "Ten"},
                    {"number": 8, "createdAt": "2025-12-01T00:00:00Z", "title": "Eight"},
                    {"number": 12, "createdAt": "2026-02-01T00:00:00Z", "title": "Twelve"},
                ]
            )
        )
        result = triage_daemon.fetch_new_issues(9)
        self.assertEqual([issue["number"] for issue in result], [10, 12])

    @patch("triage_daemon.subprocess.run")
    def test_requests_the_title_field(self, mock_run):
        """Requests title alongside number/createdAt, so it's available to print without a second call."""
        mock_run.return_value = MagicMock(stdout=json.dumps([]))
        triage_daemon.fetch_new_issues(0)
        args = mock_run.call_args[0][0]
        json_fields = args[args.index("--json") + 1]
        self.assertIn("title", json_fields.split(","))


class FetchBotPrIssuesTests(unittest.TestCase):
    """Tests for fetch_bot_pr_issues(), new in the bot PR flow."""

    @patch("triage_daemon.subprocess.run")
    def test_queries_open_issues_labelled_bot_pr(self, mock_run):
        """Calls gh issue list scoped to the BOT_PR label and returns the parsed issues."""
        mock_run.return_value = MagicMock(stdout=json.dumps([{"number": 4720, "labels": [{"name": "BOT_PR"}, {"name": "BOT_TRIAGED"}], "title": "Solis TOU bit refused"}]))
        result = triage_daemon.fetch_bot_pr_issues()
        self.assertEqual(
            result,
            [{"number": 4720, "labels": [{"name": "BOT_PR"}, {"name": "BOT_TRIAGED"}], "title": "Solis TOU bit refused"}],
        )
        args = mock_run.call_args[0][0]
        self.assertIn("--label", args)
        self.assertEqual(args[args.index("--label") + 1], "BOT_PR")
        json_fields = args[args.index("--json") + 1]
        self.assertIn("title", json_fields.split(","))

    @patch("triage_daemon.subprocess.run")
    def test_requests_a_generous_limit(self, mock_run):
        """gh issue list defaults to 30 results; request enough that BOT_PR issues aren't silently dropped."""
        mock_run.return_value = MagicMock(stdout=json.dumps([]))
        triage_daemon.fetch_bot_pr_issues()
        args = mock_run.call_args[0][0]
        self.assertIn("--limit", args)


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
        mock_run.return_value = MagicMock(stdout=json.dumps({"comments": [{"body": "This is an automated first-pass triage of..."}]}))
        self.assertTrue(triage_daemon.find_triage_comment(4720))

    @patch("triage_daemon.subprocess.run")
    def test_find_triage_comment_false_when_no_match(self, mock_run):
        """Unrelated comments don't count as a triage comment."""
        mock_run.return_value = MagicMock(stdout=json.dumps({"comments": [{"body": "thanks for the report"}]}))
        self.assertFalse(triage_daemon.find_triage_comment(4720))

    @patch("triage_daemon.subprocess.run")
    def test_find_triage_comment_scopes_to_repo(self, mock_run):
        """Always passes --repo explicitly - the daemon's cwd isn't guaranteed to be the clone."""
        mock_run.return_value = MagicMock(stdout=json.dumps({"comments": []}))
        triage_daemon.find_triage_comment(4720)
        args = mock_run.call_args[0][0]
        self.assertIn("--repo", args)
        self.assertEqual(args[args.index("--repo") + 1], "springfall2008/batpred")

    @patch("triage_daemon.subprocess.run")
    def test_backfill_triaged_label_scopes_to_repo(self, mock_run):
        """Always passes --repo explicitly - the daemon's cwd isn't guaranteed to be the clone."""
        triage_daemon.backfill_triaged_label(4720)
        args = mock_run.call_args[0][0]
        self.assertIn("--repo", args)
        self.assertEqual(args[args.index("--repo") + 1], "springfall2008/batpred")

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

    @patch("triage_daemon.subprocess.run")
    def test_has_existing_pr_scopes_to_repo(self, mock_run):
        """Always passes --repo explicitly - the daemon's cwd isn't guaranteed to be the clone."""
        mock_run.return_value = MagicMock(stdout=json.dumps([]))
        triage_daemon.has_existing_pr(4720)
        args = mock_run.call_args[0][0]
        self.assertIn("--repo", args)
        self.assertEqual(args[args.index("--repo") + 1], "springfall2008/batpred")

    @patch("triage_daemon.subprocess.run")
    def test_find_pr_number_for_issue_returns_the_number(self, mock_run):
        """A matching search result's PR number is returned, not just a bool."""
        mock_run.return_value = MagicMock(stdout=json.dumps([{"number": 4742}]))
        self.assertEqual(triage_daemon.find_pr_number_for_issue(4720), 4742)

    @patch("triage_daemon.subprocess.run")
    def test_find_pr_number_for_issue_returns_none_when_no_match(self, mock_run):
        """An empty search result means no PR exists yet for this issue."""
        mock_run.return_value = MagicMock(stdout=json.dumps([]))
        self.assertIsNone(triage_daemon.find_pr_number_for_issue(4720))


class IsActionableTests(unittest.TestCase):
    """Tests for is_actionable(), new - guards against implementing a closed,
    duplicate, question, or configuration issue after an inline /issue-triage call."""

    @patch("triage_daemon.subprocess.run")
    def test_true_when_open_and_classified_bug(self, mock_run):
        """An open issue classified bug is actionable."""
        mock_run.return_value = MagicMock(stdout=json.dumps({"state": "OPEN", "labels": [{"name": "bug"}]}))
        self.assertTrue(triage_daemon.is_actionable(4720))

    @patch("triage_daemon.subprocess.run")
    def test_true_when_open_and_classified_enhancement(self, mock_run):
        """An open issue classified enhancement is actionable."""
        mock_run.return_value = MagicMock(stdout=json.dumps({"state": "OPEN", "labels": [{"name": "enhancement"}]}))
        self.assertTrue(triage_daemon.is_actionable(4720))

    @patch("triage_daemon.subprocess.run")
    def test_false_when_closed(self, mock_run):
        """A closed issue (e.g. triaged as a duplicate and closed) is not actionable."""
        mock_run.return_value = MagicMock(stdout=json.dumps({"state": "CLOSED", "labels": [{"name": "bug"}]}))
        self.assertFalse(triage_daemon.is_actionable(4720))

    @patch("triage_daemon.subprocess.run")
    def test_false_when_classified_question(self, mock_run):
        """A question isn't something to implement a PR for."""
        mock_run.return_value = MagicMock(stdout=json.dumps({"state": "OPEN", "labels": [{"name": "question"}]}))
        self.assertFalse(triage_daemon.is_actionable(4720))

    @patch("triage_daemon.subprocess.run")
    def test_scopes_to_repo(self, mock_run):
        """Always passes --repo explicitly - the daemon's cwd isn't guaranteed to be the clone."""
        mock_run.return_value = MagicMock(stdout=json.dumps({"state": "OPEN", "labels": []}))
        triage_daemon.is_actionable(4720)
        args = mock_run.call_args[0][0]
        self.assertIn("--repo", args)
        self.assertEqual(args[args.index("--repo") + 1], "springfall2008/batpred")


class MarkPrNotActionableTests(unittest.TestCase):
    """Tests for mark_pr_not_actionable(), new - explains why no PR was attempted."""

    @patch("triage_daemon.mark_pr_failed")
    @patch("triage_daemon.subprocess.run")
    def test_comments_then_delegates_to_mark_pr_failed(self, mock_run, mock_mark_failed):
        """Posts an explanatory comment, then reuses mark_pr_failed for the label swap."""
        triage_daemon.mark_pr_not_actionable(4720)
        comment_args = mock_run.call_args[0][0]
        self.assertEqual(comment_args[:3], ["gh", "issue", "comment"])
        mock_mark_failed.assert_called_once_with(4720)


class LabelSwapTests(unittest.TestCase):
    """Tests for mark_pr_opened/mark_pr_failed, new in the bot PR flow."""

    @patch("triage_daemon.subprocess.run")
    def test_mark_pr_opened_swaps_labels(self, mock_run):
        """Removes BOT_PR and adds BOT_PR_OPENED, scoped to the configured repo."""
        mock_run.return_value = MagicMock(stdout=json.dumps([{"number": 4742}]))
        triage_daemon.mark_pr_opened(4720)
        first_call_args = mock_run.call_args_list[0].args[0]
        self.assertEqual(
            first_call_args,
            [
                "gh",
                "issue",
                "edit",
                "4720",
                "--repo",
                "springfall2008/batpred",
                "--remove-label",
                "BOT_PR",
                "--add-label",
                "BOT_PR_OPENED",
            ],
        )

    @patch("triage_daemon.subprocess.run")
    def test_mark_pr_opened_flags_the_pr_for_review(self, mock_run):
        """Once the issue's label is swapped, the PR itself is found and flagged
        BOT_REVIEW - /issue-pr's own quality gate is pre-commit and a targeted test,
        not an LLM review of the diff, so this is what actually triggers one."""
        mock_run.return_value = MagicMock(stdout=json.dumps([{"number": 4742}]))
        triage_daemon.mark_pr_opened(4720)
        calls = [call.args[0] for call in mock_run.call_args_list]
        self.assertIn(["gh", "pr", "edit", "4742", "--repo", "springfall2008/batpred", "--add-label", "BOT_REVIEW"], calls)

    @patch("triage_daemon.subprocess.run")
    def test_mark_pr_opened_skips_flagging_when_no_pr_found(self, mock_run):
        """Defensive path: an empty PR search (e.g. a race with the PR being closed
        between the caller's has_existing_pr() check and this call) must not crash
        trying to flag a PR number that doesn't exist."""
        mock_run.return_value = MagicMock(stdout=json.dumps([]))
        triage_daemon.mark_pr_opened(4720)  # must not raise
        calls = [call.args[0] for call in mock_run.call_args_list]
        self.assertFalse(any(call[:3] == ["gh", "pr", "edit"] for call in calls))

    @patch("triage_daemon.subprocess.run")
    def test_mark_pr_failed_swaps_labels(self, mock_run):
        """Removes BOT_PR and adds BOT_PR_FAILED, scoped to the configured repo."""
        triage_daemon.mark_pr_failed(4720)
        args = mock_run.call_args[0][0]
        self.assertEqual(
            args,
            [
                "gh",
                "issue",
                "edit",
                "4720",
                "--repo",
                "springfall2008/batpred",
                "--remove-label",
                "BOT_PR",
                "--add-label",
                "BOT_PR_FAILED",
            ],
        )


class FlagPrForReviewTests(unittest.TestCase):
    """Tests for flag_pr_for_review(), new - adds BOT_REVIEW to a PR directly."""

    @patch("triage_daemon.subprocess.run")
    def test_adds_bot_review_to_the_pr_scoped_to_repo(self, mock_run):
        """Adds the label to the PR (not the issue), scoped to the configured repo."""
        triage_daemon.flag_pr_for_review(4742)
        args = mock_run.call_args[0][0]
        self.assertEqual(args, ["gh", "pr", "edit", "4742", "--repo", "springfall2008/batpred", "--add-label", "BOT_REVIEW"])


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
        """The only additions are add/commit/push/pr-create/pre-commit. The push entries are
        enumerated per branch prefix rather than a bare `git push*`: an unscoped grant is what
        let a cleanup run push to upstream main on 2026-09-06."""
        base = set(triage_daemon.ALLOWED_TOOLS.split(","))
        pr = set(triage_daemon.ALLOWED_TOOLS_PR.split(","))
        self.assertEqual(
            pr - base,
            {
                "Bash(git add*)",
                "Bash(git commit*)",
                "Bash(git push)",
                "Bash(git push origin fix/*)",
                "Bash(git push -u origin fix/*)",
                "Bash(git push origin feat/*)",
                "Bash(git push -u origin feat/*)",
                "Bash(gh pr create*)",
                "Bash(./run_pre_commit*)",
                "Bash(./run_pre_commit)",
            },
        )
        self.assertNotIn("Bash(git push*)", pr)

    def test_pr_disallowed_tools_still_blocks_force_push_variants(self):
        """Even though the PR flow can push, force-push stays denied - defense in depth
        against a prompt-injected instruction attempting to rewrite history."""
        pr_denied = triage_daemon.DISALLOWED_TOOLS_PR.split(",")
        self.assertTrue(any("force" in entry for entry in pr_denied), pr_denied)
        self.assertTrue(any("-f" in entry for entry in pr_denied), pr_denied)

    def test_force_push_denials_do_not_false_positive_on_a_branch_name_containing_f(self):
        """Regression test for issue #4788: the force-push heuristic used to be
        "Bash(git push*-f*)", an unanchored substring match that read the "-f" inside
        an ordinary branch name as the force-push flag and denied a completely normal
        push. "fix/power-flow-car-outside-ct-clamp-4788" contains "-flow", which tripped
        it - the PR flow's own push step got silently denied and the branch never made
        it to origin from that run. Anchoring "-f" to its own token (spaces on both
        sides, or the end of the command) must not regress back to matching substrings
        like "-flow", "-fix" or "-format"."""
        ordinary_pushes = [
            "git push -u origin fix/power-flow-car-outside-ct-clamp-4788",
            "git push -u origin fix/auto-format-thing-1234",
            "git push -u origin feat/prefix-field-support-9001",
        ]
        force_push_denials = [rule for rule in triage_daemon.DISALLOWED_TOOLS_PR.split(",") if "force" in rule or "-f" in rule]
        for command in ordinary_pushes:
            matched = [rule for rule in force_push_denials if bash_rule_matches(rule, command)]
            self.assertEqual(matched, [], f"{command!r} was falsely denied by {matched}")

    def test_force_push_denials_still_catch_real_force_push_spellings(self):
        """The tightened force-push rules must still deny every spelling a maintainer
        or a prompt-injected instruction would actually use."""
        real_force_pushes = [
            "git push -f origin main",
            "git push origin main -f",
            "git push --force origin main",
            "git push --force-with-lease origin main",
        ]
        force_push_denials = [rule for rule in triage_daemon.DISALLOWED_TOOLS_PR.split(",") if "force" in rule or "-f" in rule]
        for command in real_force_pushes:
            matched = any(bash_rule_matches(rule, command) for rule in force_push_denials)
            self.assertTrue(matched, f"{command!r} should still be denied, but no rule in {force_push_denials} matched")

    def test_review_disallowed_tools_still_blocks_dangerous_gh_subcommands(self):
        """The BOT_REVIEW-on-PR flow keeps every dangerous gh subcommand denied,
        including generic gh api calls against another repo."""
        still_denied = [
            "Bash(gh pr merge*)",
            "Bash(gh pr close*)",
            "Bash(gh pr create*)",
            "Bash(gh repo*)",
            "Bash(gh release*)",
            "Bash(gh workflow*)",
            "Bash(gh auth*)",
            "Bash(gh secret*)",
        ]
        review_denied = triage_daemon.DISALLOWED_TOOLS_REVIEW.split(",")
        for entry in still_denied:
            self.assertIn(entry, review_denied)

    def test_review_disallowed_tools_carves_out_only_gh_api(self):
        """Only the blanket gh api denial is removed - everything else stays denied."""
        base = set(triage_daemon.DISALLOWED_TOOLS.split(","))
        review = set(triage_daemon.DISALLOWED_TOOLS_REVIEW.split(","))
        self.assertEqual(base - review, {"Bash(gh api*)"})

    def test_review_allowed_tools_does_not_inherit_the_broad_gh_grant(self):
        """Regression test for the actual bug: with "Bash(gh *)" present, removing the
        blanket gh api denial would un-restrict gh api entirely (any repo, any
        endpoint, including merge/close via REST) - a narrower allow gets no
        precedence over a broader one, only deny-wins-over-allow is a real rule.
        The scoped gh api grant only means something if this entry is absent."""
        review = triage_daemon.ALLOWED_TOOLS_REVIEW.split(",")
        self.assertNotIn("Bash(gh *)", review)

    def test_review_allowed_tools_does_not_grant_formal_review_actions(self):
        """No "gh pr review*": that would also allow --approve/--request-changes,
        a governance action beyond "post a comment"."""
        review = triage_daemon.ALLOWED_TOOLS_REVIEW.split(",")
        self.assertNotIn("Bash(gh pr review*)", review)

    def test_review_allowed_tools_still_covers_the_read_only_base(self):
        """Dropping the broad gh grant must not drop the non-gh read tools (git
        history, file reads, the scoped Edit rules) every other flow still has."""
        non_gh = set(triage_daemon._ALLOWED_TOOLS_NON_GH)
        review = set(triage_daemon.ALLOWED_TOOLS_REVIEW.split(","))
        self.assertTrue(non_gh.issubset(review))

    def test_review_allowed_tools_grants_exactly_the_expected_gh_entries(self):
        """The complete, curated gh surface for the review flow - specific
        subcommands plus the scoped api grants, nothing broader."""
        review = set(triage_daemon.ALLOWED_TOOLS_REVIEW.split(","))
        gh_entries = {entry for entry in review if entry.startswith("Bash(gh")}
        self.assertEqual(
            gh_entries,
            {
                "Bash(gh pr view*)",
                "Bash(gh pr diff*)",
                "Bash(gh pr list*)",
                "Bash(gh pr comment*)",
                "Bash(gh pr edit*)",
                "Bash(gh issue comment*)",
                "Bash(gh issue view*)",
                "Bash(gh issue list*)",
                "Bash(gh search*)",
            }
            | set(triage_daemon._REVIEW_EXTRA_ALLOWED),
        )

    def test_review_allowed_tools_covers_the_method_flag_first_gh_api_form(self):
        """The bug that silently dropped PR #4758's review: the agent wrote the POST as
        `gh api --method POST repos/...`, the canonical form for a POST, which the single
        endpoint-first prefix glob does not match - so every inline comment was denied while
        `claude -p` still exited 0. #4759's POST happened to be endpoint-first and went through."""
        review = triage_daemon.ALLOWED_TOOLS_REVIEW.split(",")
        for form in [
            "Bash(gh api repos/springfall2008/batpred/*)",
            "Bash(gh api --method POST repos/springfall2008/batpred/*)",
            "Bash(gh api --method PATCH repos/springfall2008/batpred/*)",
            "Bash(gh api -X POST repos/springfall2008/batpred/*)",
            "Bash(gh api -X PATCH repos/springfall2008/batpred/*)",
        ]:
            self.assertIn(form, review)

    def test_review_allowed_tools_covers_quoted_gh_api_endpoints(self):
        """The other observed denial: a quoted endpoint (`gh api "repos/..."`) also misses a
        prefix glob written for the bare form."""
        review = triage_daemon.ALLOWED_TOOLS_REVIEW.split(",")
        self.assertIn('Bash(gh api "repos/springfall2008/batpred/*)', review)
        self.assertIn("Bash(gh api 'repos/springfall2008/batpred/*)", review)

    def test_every_scoped_gh_api_grant_stays_pinned_to_this_repo(self):
        """Broadening the grant to cover more command forms must not broaden its reach:
        every variant still names this repo, and none degrades to a bare "gh api*"."""
        for flow in [triage_daemon.ALLOWED_TOOLS_REVIEW, triage_daemon.ALLOWED_TOOLS_CLEANUP]:
            api_entries = [entry for entry in flow.split(",") if entry.startswith("Bash(gh api")]
            self.assertTrue(api_entries)
            for entry in api_entries:
                self.assertIn("repos/springfall2008/batpred/", entry)
                self.assertTrue(entry.endswith("repos/springfall2008/batpred/*)"), entry)

    def test_scoped_gh_api_grants_never_allow_destructive_methods(self):
        """Only POST and PATCH are enumerated - the review flow creates and edits comments,
        it never needs DELETE or PUT, and spelling those out would hand it the REST routes to
        remove reviews or replace branch contents."""
        for flow in [triage_daemon.ALLOWED_TOOLS_REVIEW, triage_daemon.ALLOWED_TOOLS_CLEANUP]:
            for entry in flow.split(","):
                if entry.startswith("Bash(gh api"):
                    self.assertNotIn("DELETE", entry)
                    self.assertNotIn("PUT", entry)

    def test_cleanup_disallowed_tools_still_blocks_dangerous_gh_subcommands(self):
        """The BOT_CLEANUP flow keeps every dangerous gh subcommand denied."""
        still_denied = [
            "Bash(gh pr merge*)",
            "Bash(gh pr close*)",
            "Bash(gh repo*)",
            "Bash(gh release*)",
            "Bash(gh workflow*)",
            "Bash(gh auth*)",
            "Bash(gh secret*)",
        ]
        cleanup_denied = triage_daemon.DISALLOWED_TOOLS_CLEANUP.split(",")
        for entry in still_denied:
            self.assertIn(entry, cleanup_denied)

    def test_cleanup_disallowed_tools_still_blocks_force_push_variants(self):
        """Same defense-in-depth as the PR flow: force-push stays denied even though push is allowed."""
        cleanup_denied = triage_daemon.DISALLOWED_TOOLS_CLEANUP.split(",")
        self.assertTrue(any("force" in entry for entry in cleanup_denied), cleanup_denied)
        self.assertTrue(any("-f" in entry for entry in cleanup_denied), cleanup_denied)

    def test_cleanup_allowed_tools_does_not_inherit_the_broad_gh_grant(self):
        """Same regression as the review flow: "Bash(gh *)" must be absent, or the
        scoped gh api grant would do nothing once the blanket gh api denial is lifted."""
        cleanup = triage_daemon.ALLOWED_TOOLS_CLEANUP.split(",")
        self.assertNotIn("Bash(gh *)", cleanup)

    def test_cleanup_allowed_tools_does_not_grant_pr_create(self):
        """Cleanup pushes to the existing PR's branch - it never opens a new one, so
        it must not inherit "gh pr create*" the way the BOT_PR flow does."""
        cleanup = triage_daemon.ALLOWED_TOOLS_CLEANUP.split(",")
        self.assertNotIn("Bash(gh pr create*)", cleanup)
        self.assertIn("Bash(gh pr create*)", triage_daemon.DISALLOWED_TOOLS_CLEANUP.split(","))

    def test_cleanup_allowed_tools_still_covers_the_read_only_base(self):
        """Dropping the broad gh grant must not drop the non-gh read tools (git
        history, file reads, the scoped Edit rules) every other flow still has."""
        non_gh = set(triage_daemon._ALLOWED_TOOLS_NON_GH)
        cleanup = set(triage_daemon.ALLOWED_TOOLS_CLEANUP.split(","))
        self.assertTrue(non_gh.issubset(cleanup))

    def test_cleanup_allowed_tools_grants_exactly_the_expected_gh_entries(self):
        """The complete, curated gh surface for the cleanup flow: the review flow's
        read access, plus checkout/checks/run-log access and the scoped api grant."""
        cleanup = set(triage_daemon.ALLOWED_TOOLS_CLEANUP.split(","))
        gh_entries = {entry for entry in cleanup if entry.startswith("Bash(gh")}
        self.assertEqual(
            gh_entries,
            {
                "Bash(gh pr view*)",
                "Bash(gh pr diff*)",
                "Bash(gh pr list*)",
                "Bash(gh pr comment*)",
                "Bash(gh pr edit*)",
                "Bash(gh issue comment*)",
                "Bash(gh issue view*)",
                "Bash(gh issue list*)",
                "Bash(gh search*)",
                "Bash(gh pr checkout*)",
                "Bash(gh pr checks*)",
                "Bash(gh run view*)",
                "Bash(gh run list*)",
            }
            | set(triage_daemon._REVIEW_EXTRA_ALLOWED),
        )

    def test_cleanup_allowed_tools_covers_the_method_flag_first_gh_api_form(self):
        """Cleanup shares the review flow's scoped api grant, so it inherits the same
        command-form coverage rather than only the endpoint-first spelling."""
        cleanup = triage_daemon.ALLOWED_TOOLS_CLEANUP.split(",")
        self.assertIn("Bash(gh api --method POST repos/springfall2008/batpred/*)", cleanup)
        self.assertIn("Bash(gh api repos/springfall2008/batpred/*)", cleanup)

    def test_cleanup_allowed_tools_grants_write_access(self):
        """Commit/push/pre-commit, matching the PR flow's write capability - the
        merge grant is checked separately below, since it's a set of enumerated
        spellings rather than a single entry."""
        cleanup = set(triage_daemon.ALLOWED_TOOLS_CLEANUP.split(","))
        self.assertTrue(
            {
                "Bash(git add*)",
                "Bash(git commit*)",
                "Bash(git push)",
                "Bash(./run_pre_commit*)",
                "Bash(./run_pre_commit)",
            }.issubset(cleanup)
        )
        # Only the bare form: `gh pr checkout` has already set the branch's upstream, so
        # cleanup never needs to name a remote or a ref.
        self.assertNotIn("Bash(git push*)", cleanup)

    def test_cleanup_is_the_only_flow_granted_git_merge(self):
        """git merge is only needed to sync a checked-out PR branch with main - no
        other flow checks out an existing branch that can be behind, so granting it
        more broadly would expand the permission surface with no matching use-case."""
        cleanup = triage_daemon.ALLOWED_TOOLS_CLEANUP.split(",")
        for entry in triage_daemon._CLEANUP_EXTRA_MERGE:
            self.assertIn(entry, cleanup)
        for flow in [triage_daemon.ALLOWED_TOOLS, triage_daemon.ALLOWED_TOOLS_PR, triage_daemon.ALLOWED_TOOLS_REVIEW]:
            merge_entries = [entry for entry in flow.split(",") if entry.startswith("Bash(git merge")]
            self.assertEqual(merge_entries, [])

    def test_cleanup_merge_grant_is_scoped_to_origin_main(self):
        """Regression test for the Copilot review on PR #4882: a bare "Bash(git
        merge*)" would let the agent merge any ref, contradicting pr-cleanup/SKILL.md's
        guardrail that it should only ever merge origin/main. Every enumerated entry
        must name origin/main explicitly, or be the exact --abort escape hatch."""
        for entry in triage_daemon._CLEANUP_EXTRA_MERGE:
            self.assertTrue("origin/main" in entry or entry == "Bash(git merge --abort)", entry)

    def test_cleanup_merge_grant_covers_a_flag_before_the_ref(self):
        """Regression test for the same Copilot review comment: prefix-glob matching
        is literal, so "git merge --no-edit origin/main" - the exact form SKILL.md's
        step 2 instructs, to avoid hanging on an interactive editor prompt - needs its
        own entry rather than relying on a bare "git merge origin/main*" rule to cover
        a flag that comes before the ref."""
        self.assertIn("Bash(git merge --no-edit origin/main*)", triage_daemon._CLEANUP_EXTRA_MERGE)


class GhApiFormPromptTests(unittest.TestCase):
    """Tests for GH_API_ENDPOINT_FIRST_PROMPT - the belt-and-braces half of the #4758 fix.
    The allowlist covers the command forms we thought of; this steers the agent onto the one
    form we know is covered, which matters because /code-review is a built-in skill whose
    command spellings cannot be edited in a SKILL.md we own."""

    def test_names_the_repo_and_the_endpoint_first_rule(self):
        """The prompt has to be concrete enough to copy: it names this repo and states that
        the endpoint goes immediately after `gh api`."""
        prompt = triage_daemon.GH_API_ENDPOINT_FIRST_PROMPT
        self.assertIn("springfall2008/batpred", prompt)
        self.assertIn("gh api", prompt)

    def test_calls_out_each_form_observed_to_be_denied(self):
        """Every spelling seen denied in the #4758/#4759 transcripts is named explicitly:
        a method flag before the endpoint, and a quoted endpoint."""
        prompt = triage_daemon.GH_API_ENDPOINT_FIRST_PROMPT
        self.assertIn("--method", prompt)
        self.assertIn("-X", prompt)
        self.assertIn("-H", prompt)

    def test_tells_the_agent_to_report_a_denial_rather_than_print_findings(self):
        """The #4758 run degraded to printing the comments it could not post, which read as a
        completed review in the log. The prompt asks for a denial to be stated plainly."""
        self.assertIn("denied", triage_daemon.GH_API_ENDPOINT_FIRST_PROMPT)

    def test_requires_disclosure_on_every_posted_comment_or_reply(self):
        """/code-review's own instructions live in a skill we don't own, so this appended
        prompt is the only lever available to make its inline comments disclose they're
        automated - and it doubles as a belt-and-braces backup for /pr-cleanup's replies,
        which already ask for disclosure directly in their own SKILL.md."""
        prompt = triage_daemon.GH_API_ENDPOINT_FIRST_PROMPT
        self.assertIn("must open with", prompt)
        self.assertIn("Automated comment from the triage bot", prompt)


class PrReviewActivityCountTests(unittest.TestCase):
    """Tests for pr_review_activity_count(), new - the evidence check behind verify-then-label."""

    @patch("triage_daemon.subprocess.run")
    def test_sums_reviews_inline_comments_and_pr_comments(self, mock_run):
        """All three places a review body can land are counted, because /code-review may post
        a formal review, inline comments, or a plain PR comment depending on what it can reach."""
        mock_run.return_value = MagicMock(stdout="2\n", returncode=0)
        self.assertEqual(triage_daemon.pr_review_activity_count(4742), 6)
        endpoints = [call.args[0][2] for call in mock_run.call_args_list]
        self.assertEqual(
            endpoints,
            [
                "repos/springfall2008/batpred/pulls/4742/reviews",
                "repos/springfall2008/batpred/pulls/4742/comments",
                "repos/springfall2008/batpred/issues/4742/comments",
            ],
        )

    @patch("triage_daemon.subprocess.run")
    def test_sums_across_paginated_pages(self, mock_run):
        """--paginate emits one length per page, so the counts have to be added rather than
        the last one taken - otherwise a busy PR under-counts and reads as "posted nothing"."""
        mock_run.return_value = MagicMock(stdout="30\n30\n4\n", returncode=0)
        self.assertEqual(triage_daemon.pr_review_activity_count(4742), 192)

    @patch("triage_daemon.subprocess.run")
    def test_handles_an_empty_response(self, mock_run):
        """A PR with no reviews at all counts as zero, not a crash."""
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        self.assertEqual(triage_daemon.pr_review_activity_count(4742), 0)


class SyncRepoTests(unittest.TestCase):
    """Tests for sync_repo(), modified to always return to main first."""

    @patch("triage_daemon.subprocess.run")
    def test_checks_out_main_before_resetting(self, mock_run):
        """A previous crashed BOT_PR run can leave the clone on a fix/*|feat/* branch;
        sync_repo must switch back to main before reset --hard, or it resets the wrong
        branch and leaves the clone stuck off main."""
        triage_daemon.sync_repo()
        calls = [call.args[0] for call in mock_run.call_args_list]
        checkout_call = ["git", "-C", str(triage_daemon.CLONE_DIR), "checkout", "main"]
        reset_call = ["git", "-C", str(triage_daemon.CLONE_DIR), "reset", "--hard", "origin/main"]
        self.assertIn(checkout_call, calls)
        self.assertLess(calls.index(checkout_call), calls.index(reset_call))


class OllamaContextWindowTests(unittest.TestCase):
    """Claude Code does not recognise the Ollama model names and assumes a 200k window,
    auto-compacting to fit. Each compaction costs turns, which is a plausible route to the
    "Reached max turns (150)" that failed the #4992 cleanup."""

    def setUp(self):
        """Start from no Ollama model, regardless of test order, and a clean environment."""
        for name in ("OLLAMA_MODEL", "OLLAMA_REVIEW_MODEL"):
            patcher = patch.object(triage_daemon, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(triage_daemon.os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        triage_daemon.os.environ.pop("CLAUDE_CODE_MAX_CONTEXT_TOKENS", None)

    def test_a_known_model_gets_its_real_window(self):
        """The whole point: stop compacting a 1M-token model as though it held 200k."""
        with patch.object(triage_daemon, "OLLAMA_MODEL", "glm-5.3-flash:cloud"):
            env = triage_daemon.claude_env()
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "1000000")

    def test_an_unknown_model_is_left_alone(self):
        """Overstating a window is worse than understating it: too low only compacts early,
        too high lets a request run past what the model accepts and fail outright. An
        unlisted model keeps Claude Code's own assumption rather than inheriting 1M."""
        with patch.object(triage_daemon, "OLLAMA_MODEL", "some-small-model:7b"):
            env = triage_daemon.claude_env()
        self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", env)

    def test_an_operator_override_wins(self):
        """A value exported for a one-off run must not be silently replaced."""
        triage_daemon.os.environ["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "250000"
        self.addCleanup(triage_daemon.os.environ.pop, "CLAUDE_CODE_MAX_CONTEXT_TOKENS", None)
        with patch.object(triage_daemon, "OLLAMA_MODEL", "glm-5.3-flash:cloud"):
            env = triage_daemon.claude_env()
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "250000")

    def test_no_window_is_set_without_an_ollama_model(self):
        """The default Claude route inherits the daemon's environment untouched - claude_env()
        returns None there, so there is nothing to set a window on."""
        self.assertIsNone(triage_daemon.claude_env())

    def test_the_review_only_route_gets_it_too(self):
        """--ollama_review is how the daemon is actually run, so the window has to follow that
        path as well as --ollama."""
        with patch.object(triage_daemon, "OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud"):
            env = triage_daemon.claude_env(review_only=True)
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "1000000")


class ProcessNewIssueTests(unittest.TestCase):
    """The issue-triage orchestrator. Before it existed, triage() sat bare in the main loop
    and a failure escaped the whole poll cycle - retrying the same issue forever while the
    PR, review, cleanup and journal flows behind it never ran (#5004, 2026-09-08)."""

    def setUp(self):
        """Patch every collaborator process_new_issue() calls."""
        self.patches = {}
        for name in ["sync_repo", "reset_scratch", "triage", "mark_triage_failed", "save_state"]:
            patcher = patch.object(triage_daemon, name)
            self.patches[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.issue = {"number": 5004, "title": "Ghost EV load"}

    def _fail(self):
        self.patches["triage"].side_effect = subprocess.CalledProcessError(1, ["claude"])

    def test_a_successful_triage_advances_the_watermark(self):
        """The ordinary path has to keep working."""
        state = {"last_processed": 5003}
        self.assertTrue(triage_daemon.process_new_issue(self.issue, state))
        self.assertEqual(state["last_processed"], 5004)
        self.patches["mark_triage_failed"].assert_not_called()

    def test_a_failure_does_not_escape_to_the_caller(self):
        """The regression itself: the exception used to abandon the whole poll cycle, so the
        cleanup, review and journal flows queued behind this issue never ran."""
        self._fail()
        state = {"last_processed": 5003}
        try:
            result = triage_daemon.process_new_issue(self.issue, state)
        except subprocess.CalledProcessError:
            self.fail("process_new_issue must not let a failed triage escape the poll cycle")
        self.assertFalse(result)

    def test_a_failure_below_the_limit_keeps_the_watermark_and_counts_the_attempt(self):
        """Retrying is worth doing - #4899 and #5003 both passed on their third attempt - so
        the issue stays at the head of the queue until its attempts are spent."""
        self._fail()
        state = {"last_processed": 5003}
        self.assertFalse(triage_daemon.process_new_issue(self.issue, state))
        self.assertEqual(state["last_processed"], 5003)
        self.assertEqual(state["triage_attempts"]["5004"], 1)
        self.patches["mark_triage_failed"].assert_not_called()

    def test_the_last_attempt_marks_the_issue_and_moves_past_it(self):
        """Unbounded retrying is what burned a full run every ten minutes."""
        self._fail()
        state = {"last_processed": 5003, "triage_attempts": {"5004": triage_daemon.TRIAGE_MAX_ATTEMPTS - 1}}
        self.assertTrue(triage_daemon.process_new_issue(self.issue, state))
        self.assertEqual(state["last_processed"], 5004)
        self.patches["mark_triage_failed"].assert_called_once_with(5004, triage_daemon.TRIAGE_MAX_ATTEMPTS)
        self.assertNotIn("5004", state["triage_attempts"])

    def test_a_failing_mark_does_not_abort_the_cycle(self):
        """mark_triage_failed() shells out to gh with check=True. If that raised here it would
        escape to the main loop and abort the rest of the poll cycle with the watermark still
        behind - reintroducing the exact bug at the one point we have decided to move on."""
        self._fail()
        self.patches["mark_triage_failed"].side_effect = subprocess.CalledProcessError(1, ["gh"])
        state = {"last_processed": 5003, "triage_attempts": {"5004": triage_daemon.TRIAGE_MAX_ATTEMPTS - 1}}
        try:
            result = triage_daemon.process_new_issue(self.issue, state)
        except subprocess.CalledProcessError:
            self.fail("a failed mark_triage_failed() must not escape process_new_issue")
        self.assertTrue(result)
        self.assertEqual(state["last_processed"], 5004, "the watermark must advance even when labelling failed")

    def test_a_success_after_earlier_failures_clears_the_counter(self):
        """Otherwise a later unrelated failure would inherit a nearly-spent budget."""
        state = {"last_processed": 5003, "triage_attempts": {"5004": 2}}
        self.assertTrue(triage_daemon.process_new_issue(self.issue, state))
        self.assertNotIn("5004", state["triage_attempts"])
        self.assertEqual(state["last_processed"], 5004)

    def test_a_pending_retry_is_persisted(self):
        """The count has to survive a daemon restart, or the limit never binds."""
        self._fail()
        state = {"last_processed": 5003}
        triage_daemon.process_new_issue(self.issue, state)
        self.patches["save_state"].assert_called_once_with(state)

    def test_state_without_the_counter_still_works(self):
        """Existing state.json files predate triage_attempts."""
        self._fail()
        state = {"last_processed": 5003}
        triage_daemon.process_new_issue(self.issue, state)
        self.assertEqual(state["triage_attempts"], {"5004": 1})


class MarkTriageFailedTests(unittest.TestCase):
    """What a maintainer sees when the bot gives up on an issue."""

    def test_comments_and_labels_the_issue(self):
        """Silently skipping would leave the issue looking untriaged with no explanation."""
        with patch("triage_daemon.subprocess.run") as mock_run:
            triage_daemon.mark_triage_failed(5004, 3)
            commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertTrue(any("comment" in c for c in commands))
        label_cmd = next(c for c in commands if "edit" in c)
        self.assertIn("BOT_FAILED", label_cmd)

    def test_the_comment_says_how_to_retry(self):
        """The watermark has moved past the issue, so nothing re-triages it on its own."""
        with patch("triage_daemon.subprocess.run") as mock_run:
            triage_daemon.mark_triage_failed(5004, 3)
            body = next(c for c in (call.args[0] for call in mock_run.call_args_list) if "comment" in c)[-1]
        self.assertIn("BOT_REVIEW", body)
        self.assertTrue(body.startswith("Automated "))

    def test_the_comment_says_to_remove_bot_failed_first(self):
        """Nothing in this file ever removes BOT_FAILED, and a successful follow-up clears only
        BOT_REVIEW - so a maintainer who adds BOT_REVIEW while BOT_FAILED is still on ends up
        with a triaged issue permanently labelled as failed. Every other marker here says to
        remove it first; this one has to as well."""
        with patch("triage_daemon.subprocess.run") as mock_run:
            triage_daemon.mark_triage_failed(5004, 3)
            body = next(c for c in (call.args[0] for call in mock_run.call_args_list) if "comment" in c)[-1]
        self.assertIn("Remove `BOT_FAILED`", body)
        self.assertLess(body.index("BOT_FAILED"), body.index("BOT_REVIEW"), "the removal must be stated before the label to add")


class GitnexusIndexTests(DaemonPathsTestCase):
    """CLAUDE.md requires an impact() call before editing any symbol. Until the MCP denial was
    lifted no flow could make one, and runs said so - they grepped for callers instead."""

    def _install_runner(self):
        runner = triage_daemon.GITNEXUS_RUNNER
        runner.parent.mkdir(parents=True, exist_ok=True)
        runner.write_text("// stub")

    def test_nothing_runs_without_the_runner(self):
        """A clone with no .gitnexus/ must degrade quietly, not fail the flow."""
        with patch("triage_daemon.subprocess.run") as mock_run:
            triage_daemon.refresh_gitnexus_index()
        mock_run.assert_not_called()

    def test_analyze_runs_when_head_has_moved(self):
        """The index has to describe the tree the flow is about to read."""
        self._install_runner()
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.side_effect = [MagicMock(returncode=0, stdout="abc1234\n"), MagicMock(returncode=0, stdout="")]
            triage_daemon.refresh_gitnexus_index()
            commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertIn("analyze", commands[-1])
        self.assertEqual(triage_daemon.GITNEXUS_HEAD_FILE.read_text(), "abc1234")

    def test_analyze_is_skipped_when_head_is_unchanged(self):
        """analyze takes ~37s and sync_repo() runs before every flow, so an unguarded call
        would spend minutes an hour rebuilding an index for a tree that had not moved."""
        self._install_runner()
        triage_daemon.GITNEXUS_HEAD_FILE.write_text("abc1234")
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc1234\n")
            triage_daemon.refresh_gitnexus_index()
            commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertFalse(any("analyze" in c for c in commands))

    def test_a_failed_analyze_does_not_stop_the_flow(self):
        """A stale index degrades an answer; a raised exception would stop the daemon."""
        self._install_runner()
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.side_effect = [MagicMock(returncode=0, stdout="abc1234\n"), MagicMock(returncode=1, stdout="")]
            triage_daemon.refresh_gitnexus_index()
        self.assertFalse(triage_daemon.GITNEXUS_HEAD_FILE.exists(), "a failed analyze must not record the head as indexed")

    def test_an_unreadable_head_leaves_the_index_alone(self):
        """Without a HEAD there is no way to tell whether the index is stale, and an empty
        marker would never match - re-analyzing on every sync from then on."""
        self._install_runner()
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            triage_daemon.refresh_gitnexus_index()
            commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertFalse(any("analyze" in c for c in commands))
        self.assertFalse(triage_daemon.GITNEXUS_HEAD_FILE.exists(), "an empty head must never be recorded")

    @patch("builtins.print")
    def test_a_missing_runner_is_reported_once_not_every_sync(self, mock_print):
        """sync_repo() runs before every flow, so an unconditional warning would print the same
        line every few minutes forever."""
        with patch.object(triage_daemon, "_GITNEXUS_RUNNER_WARNED", False):
            triage_daemon.refresh_gitnexus_index()
            triage_daemon.refresh_gitnexus_index()
            triage_daemon.refresh_gitnexus_index()
            printed = [str(call.args[0]) for call in mock_print.call_args_list if "no runner" in str(call.args[0])]
        self.assertEqual(len(printed), 1)

    def test_sync_repo_refreshes_the_index(self):
        """The point of the change: every flow starts against an index of the tree it will read."""
        with patch("triage_daemon.subprocess.run"), patch("triage_daemon.install_push_guard"), patch("triage_daemon.refresh_gitnexus_index") as mock_refresh:
            triage_daemon.sync_repo()
        mock_refresh.assert_called_once()


class McpScopeTests(DaemonPathsTestCase):
    """gitnexus, and nothing else. The operator's own configuration carries other servers and
    a bot session has no business reaching those."""

    def test_the_config_names_only_gitnexus(self):
        """--strict-mcp-config pins the subprocess to whatever is in this file."""
        with patch("triage_daemon.shutil.which", return_value="/usr/local/bin/gitnexus"):
            self.assertTrue(triage_daemon.write_mcp_config())
        config = json.loads(triage_daemon.MCP_CONFIG_FILE.read_text())
        self.assertEqual(list(config["mcpServers"]), ["gitnexus"])

    def test_a_missing_binary_degrades_rather_than_breaking(self):
        """No gitnexus on PATH means no flags and flows that run as they did before."""
        with patch("triage_daemon.shutil.which", return_value=None):
            self.assertFalse(triage_daemon.write_mcp_config())
        self.assertFalse(triage_daemon.MCP_CONFIG_FILE.exists())
        self.assertEqual(triage_daemon.claude_mcp_args(), [])

    def test_a_stale_config_is_removed_when_the_binary_goes_away(self):
        """claude_mcp_args() keys off the file existing, so a config written while gitnexus was
        installed would keep being passed with --strict-mcp-config, pointing every flow at a
        command that is gone."""
        with patch("triage_daemon.shutil.which", return_value="/usr/local/bin/gitnexus"):
            triage_daemon.write_mcp_config()
        self.assertTrue(triage_daemon.MCP_CONFIG_FILE.exists())
        with patch("triage_daemon.shutil.which", return_value=None):
            triage_daemon.write_mcp_config()
        self.assertFalse(triage_daemon.MCP_CONFIG_FILE.exists())
        self.assertEqual(triage_daemon.claude_mcp_args(), [])

    def test_the_pin_is_strict(self):
        """Without --strict-mcp-config the subprocess inherits every server the operator has."""
        triage_daemon.MCP_CONFIG_FILE.write_text("{}")
        args = triage_daemon.claude_mcp_args()
        self.assertIn("--strict-mcp-config", args)
        self.assertEqual(args[args.index("--mcp-config") + 1], str(triage_daemon.MCP_CONFIG_FILE))


class McpPermissionTests(unittest.TestCase):
    """The second, independent layer: even if another server were loaded, its tools are not
    permitted. Under dontAsk anything not named in the allowlist is denied."""

    FLOWS = ("ALLOWED_TOOLS", "ALLOWED_TOOLS_PR", "ALLOWED_TOOLS_REVIEW", "ALLOWED_TOOLS_CLEANUP", "ALLOWED_TOOLS_JOURNAL")

    def test_every_flow_may_use_gitnexus(self):
        """CLAUDE.md asks for impact() before any symbol edit - every flow needs the tools."""
        for name in self.FLOWS:
            with self.subTest(flow=name):
                self.assertIn("mcp__gitnexus__*", getattr(triage_daemon, name).split(","))

    def test_no_flow_gets_a_blanket_mcp_grant(self):
        """A blanket grant would reach the operator's Slack and Drive servers."""
        for name in self.FLOWS:
            with self.subTest(flow=name):
                self.assertNotIn("mcp__*", getattr(triage_daemon, name).split(","))


class WriteGrantTests(unittest.TestCase):
    """Shell redirection (`cmd > file`) is refused with a Bash grant alone and permitted once
    Write is present - checked both ways against claude 2.1.251. That restriction is what made
    runs invent workarounds, and for most flows it was protecting nothing."""

    WRITE_FLOWS = ("ALLOWED_TOOLS", "ALLOWED_TOOLS_PR", "ALLOWED_TOOLS_REVIEW", "ALLOWED_TOOLS_CLEANUP")

    def test_the_flows_that_already_edit_the_clone_get_write(self):
        """Not new reach: each of these already holds clone-wide Edit, so Write only removes
        the need to work around a restriction that never bounded them."""
        for name in self.WRITE_FLOWS:
            with self.subTest(flow=name):
                allowed = getattr(triage_daemon, name).split(",")
                self.assertIn("Write", allowed)
                self.assertIn(f"Edit({triage_daemon.EDIT_SCOPE})", allowed, "Write is only defensible where clone-wide Edit already is")

    def test_the_journal_flush_does_not_get_write(self):
        """The one flow whose edit scope is two exact files rather than the clone, precisely
        because it is also the one that can push. Write would let it author anything in the
        clone and commit it, which is what the narrow scope exists to prevent."""
        self.assertNotIn("Write", triage_daemon.ALLOWED_TOOLS_JOURNAL.split(","))

    def test_the_journal_flush_still_has_its_narrow_edit_scope(self):
        """Guards the pairing: dropping Write is only safe while the edit scope stays narrow."""
        allowed = triage_daemon.ALLOWED_TOOLS_JOURNAL.split(",")
        self.assertNotIn(f"Edit({triage_daemon.EDIT_SCOPE})", allowed)


class CleanupModelTests(unittest.TestCase):
    """cleanup_pr edits a maintainer's branch and pushes it."""

    def test_cleanup_runs_on_claude_not_the_review_model(self):
        """The worst outputs of 2026-09 came from this flow on the review model: overriding a
        contributor's deliberate spelling on circular evidence (#4846), and improvising a push
        to main after a fork branch refused one."""
        source = Path(triage_daemon.__file__).read_text()
        start = source.index("def cleanup_pr(pr_number):")
        block = source[start : source.index("\ndef ", start + 10)]
        self.assertNotIn("review_only=True", block)

    def test_the_read_only_flows_stay_on_the_review_model(self):
        """Triage and review do not write to the repo; the cost case for Claude is weaker."""
        source = Path(triage_daemon.__file__).read_text()
        for flow in ("def triage(issue_number):", "def review_pr(pr_number"):
            with self.subTest(flow=flow):
                start = source.index(flow)
                block = source[start : source.index("\ndef ", start + 10)]
                self.assertIn("review_only=True", block)


class EffectiveOllamaModelTests(unittest.TestCase):
    """Tests for effective_ollama_model(), new - the priority logic behind --ollama
    (every claude invocation) vs --ollama_review (review-only invocations)."""

    def setUp(self):
        """Every test starts from the no-flag default, regardless of test order."""
        for name in ("OLLAMA_MODEL", "OLLAMA_REVIEW_MODEL"):
            patcher = patch.object(triage_daemon, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_none_by_default(self):
        """With neither flag set, every invocation uses the default Claude model."""
        self.assertIsNone(triage_daemon.effective_ollama_model())
        self.assertIsNone(triage_daemon.effective_ollama_model(review_only=True))

    def test_ollama_applies_regardless_of_review_only(self):
        """--ollama (OLLAMA_MODEL) covers every invocation, including PR creation."""
        with patch.object(triage_daemon, "OLLAMA_MODEL", "glm-5.3-flash:cloud"):
            self.assertEqual(triage_daemon.effective_ollama_model(review_only=False), "glm-5.3-flash:cloud")
            self.assertEqual(triage_daemon.effective_ollama_model(review_only=True), "glm-5.3-flash:cloud")

    def test_ollama_review_only_applies_when_review_only_is_true(self):
        """--ollama_review (OLLAMA_REVIEW_MODEL) is ignored unless the caller marks
        this invocation review_only - this is what keeps PR creation (which never
        passes review_only=True) on the default Claude model."""
        with patch.object(triage_daemon, "OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud"):
            self.assertIsNone(triage_daemon.effective_ollama_model(review_only=False))
            self.assertEqual(triage_daemon.effective_ollama_model(review_only=True), "glm-5.3-flash:cloud")

    def test_ollama_takes_precedence_over_ollama_review(self):
        """If both somehow end up set, the blanket --ollama model wins."""
        with patch.object(triage_daemon, "OLLAMA_MODEL", "full-model"), patch.object(triage_daemon, "OLLAMA_REVIEW_MODEL", "review-model"):
            self.assertEqual(triage_daemon.effective_ollama_model(review_only=True), "full-model")


class ClaudeModelArgsTests(unittest.TestCase):
    """Tests for claude_model_args(), new - the --ollama/--ollama_review-to---model
    plumbing shared by every 'claude' invocation (triage, triage_followup, create_pr,
    review_pr, cleanup_pr)."""

    def setUp(self):
        """Every test starts from the no-flag default, regardless of test order."""
        for name in ("OLLAMA_MODEL", "OLLAMA_REVIEW_MODEL"):
            patcher = patch.object(triage_daemon, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_empty_by_default(self):
        """With no --ollama, no --model flag is added to any claude invocation."""
        self.assertEqual(triage_daemon.claude_model_args(), [])

    def test_selects_the_configured_model(self):
        """--ollama's model name is passed straight through as --model, :cloud suffix included."""
        with patch.object(triage_daemon, "OLLAMA_MODEL", "glm-5.3-flash:cloud"):
            self.assertEqual(triage_daemon.claude_model_args(), ["--model", "glm-5.3-flash:cloud"])

    def test_review_only_model_used_when_review_only_true(self):
        """claude_model_args(review_only=True) picks up --ollama_review when set -
        the call form triage()/triage_followup()/review_pr()/cleanup_pr() use."""
        with patch.object(triage_daemon, "OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud"):
            self.assertEqual(triage_daemon.claude_model_args(review_only=True), ["--model", "glm-5.3-flash:cloud"])

    def test_review_only_model_ignored_by_default(self):
        """claude_model_args() with no argument - the form create_pr() uses - ignores
        --ollama_review, so PR creation is unaffected by it."""
        with patch.object(triage_daemon, "OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud"):
            self.assertEqual(triage_daemon.claude_model_args(), [])


class ClaudeEnvTests(unittest.TestCase):
    """Tests for claude_env(), new - the Anthropic-compatible env overrides Ollama's
    Claude Code integration documents (https://docs.ollama.com/integrations/claude-code)."""

    def setUp(self):
        """Every test starts from the no-flag default, regardless of test order."""
        for name in ("OLLAMA_MODEL", "OLLAMA_REVIEW_MODEL"):
            patcher = patch.object(triage_daemon, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_none_by_default(self):
        """With no --ollama, env=None so subprocess.run() inherits the daemon's own
        environment unchanged - no ANTHROPIC_* overrides pointing at Ollama."""
        self.assertIsNone(triage_daemon.claude_env())

    def test_adds_the_documented_overrides_when_configured(self):
        """--ollama sets exactly the three env vars Ollama's integration guide documents."""
        with patch.object(triage_daemon, "OLLAMA_MODEL", "glm-5.3-flash:cloud"):
            env = triage_daemon.claude_env()
        self.assertEqual(env["ANTHROPIC_BASE_URL"], triage_daemon.OLLAMA_BASE_URL)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "ollama")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")

    def test_preserves_the_rest_of_the_process_environment(self):
        """The overrides sit on top of the daemon's own environment, not a bare dict -
        the gh/git subcommands inside the claude session still need PATH, HOME, etc."""
        with patch.object(triage_daemon, "OLLAMA_MODEL", "glm-5.3-flash:cloud"), patch.dict("os.environ", {"SOME_OTHER_VAR": "keep-me"}):
            env = triage_daemon.claude_env()
        self.assertEqual(env.get("SOME_OTHER_VAR"), "keep-me")

    def test_review_only_env_used_when_review_only_true(self):
        """claude_env(review_only=True) picks up --ollama_review when set."""
        with patch.object(triage_daemon, "OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud"):
            env = triage_daemon.claude_env(review_only=True)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], triage_daemon.OLLAMA_BASE_URL)

    def test_review_only_env_ignored_by_default(self):
        """claude_env() with no argument - the form create_pr() uses - ignores --ollama_review."""
        with patch.object(triage_daemon, "OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud"):
            env = triage_daemon.claude_env()
        self.assertIsNone(env)


class ClaudeBudgetArgsTests(unittest.TestCase):
    """Tests for claude_budget_args(), new - regression tests for issue #4881: a
    triage run against an Ollama model completed its real work and then kept running
    until Claude Code's (Anthropic-priced) cost estimate crossed --max-budget-usd,
    aborting with a false failure that made the daemon retry an already-finished issue."""

    def setUp(self):
        """Every test starts from the no-flag default, regardless of test order."""
        for name in ("OLLAMA_MODEL", "OLLAMA_REVIEW_MODEL"):
            patcher = patch.object(triage_daemon, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_caps_spend_by_default(self):
        """With no --ollama flag, the budget cap applies as before."""
        self.assertEqual(triage_daemon.claude_budget_args("10.00"), ["--max-budget-usd", "10.00"])

    def test_omitted_when_ollama_is_active(self):
        """--ollama's cost estimate is meaningless for a non-Anthropic model, so the
        cap is dropped entirely rather than left in place to fire falsely."""
        with patch.object(triage_daemon, "OLLAMA_MODEL", "glm-5.3-flash:cloud"):
            self.assertEqual(triage_daemon.claude_budget_args("10.00", review_only=True), [])

    def test_omitted_when_ollama_review_is_active_for_a_review_only_call(self):
        """Same as --ollama, for the review-only flows --ollama_review covers."""
        with patch.object(triage_daemon, "OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud"):
            self.assertEqual(triage_daemon.claude_budget_args("10.00", review_only=True), [])

    def test_still_applies_to_pr_creation_under_ollama_review(self):
        """--ollama_review never touches create_pr() (review_only=False there) - it
        still runs on the real Claude model, so its budget cap must still apply."""
        with patch.object(triage_daemon, "OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud"):
            self.assertEqual(triage_daemon.claude_budget_args("25.00"), ["--max-budget-usd", "25.00"])

    def test_omitted_from_pr_creation_under_the_blanket_ollama_flag(self):
        """Unlike --ollama_review, --ollama covers every invocation including
        create_pr() - its budget cap is dropped there too."""
        with patch.object(triage_daemon, "OLLAMA_MODEL", "glm-5.3-flash:cloud"):
            self.assertEqual(triage_daemon.claude_budget_args("25.00"), [])


class ParseArgsTests(unittest.TestCase):
    """Tests for parse_args(), new - the --ollama/--ollama_review CLI flags."""

    def test_defaults_to_no_ollama_model(self):
        """Without either flag, both args are None - every claude invocation uses the default model."""
        with patch("sys.argv", ["triage_daemon.py"]):
            args = triage_daemon.parse_args()
        self.assertIsNone(args.ollama)
        self.assertIsNone(args.ollama_review)

    def test_parses_the_ollama_model_flag(self):
        """--ollama <model> is captured verbatim, :cloud suffix included."""
        with patch("sys.argv", ["triage_daemon.py", "--ollama", "glm-5.3-flash:cloud"]):
            args = triage_daemon.parse_args()
        self.assertEqual(args.ollama, "glm-5.3-flash:cloud")
        self.assertIsNone(args.ollama_review)

    def test_parses_the_ollama_review_model_flag(self):
        """--ollama_review <model> is captured verbatim, separately from --ollama."""
        with patch("sys.argv", ["triage_daemon.py", "--ollama_review", "glm-5.3-flash:cloud"]):
            args = triage_daemon.parse_args()
        self.assertEqual(args.ollama_review, "glm-5.3-flash:cloud")
        self.assertIsNone(args.ollama)

    def test_ollama_and_ollama_review_are_mutually_exclusive(self):
        """Passing both is a usage error rather than a silently-resolved precedence -
        --ollama already covers every flow --ollama_review does, so combining them
        would just be ambiguous about which one the user actually meant."""
        with patch("sys.argv", ["triage_daemon.py", "--ollama", "model-a", "--ollama_review", "model-b"]):
            with self.assertRaises(SystemExit):
                triage_daemon.parse_args()


class TriageTests(DaemonPathsTestCase):
    """Tests for triage(), exercised directly here for the first time - previously
    only covered indirectly through the orchestrators, which mock it out."""

    @patch("triage_daemon.subprocess.run")
    def test_invokes_claude_with_the_triage_permission_set(self, mock_run):
        """Runs the /issue-triage skill with the read-only allow/deny lists."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.triage(4720)
        cmd = mock_run.call_args[0][0]
        self.assertIn("/issue-triage 4720", cmd[2])
        self.assertIn(triage_daemon.ALLOWED_TOOLS, cmd)
        self.assertIn(triage_daemon.DISALLOWED_TOOLS, cmd)

    @patch("triage_daemon.subprocess.run")
    def test_no_model_flag_or_env_override_by_default(self, mock_run):
        """Without --ollama, the invocation is unchanged: no --model flag, env=None
        so the claude subprocess talks to Anthropic's API as normal."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.triage(4720)
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--model", cmd)
        self.assertIsNone(mock_run.call_args.kwargs["env"])

    @patch("triage_daemon.subprocess.run")
    def test_adds_the_ollama_model_flag_and_env_when_configured(self, mock_run):
        """--ollama appends --model <name> to the cmd and routes the subprocess at
        Ollama's Claude Code compatible endpoint via the env overrides."""
        self._patch("OLLAMA_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.triage(4720)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "glm-5.3-flash:cloud")
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], triage_daemon.OLLAMA_BASE_URL)

    @patch("triage_daemon.subprocess.run")
    def test_ollama_review_model_also_applies_to_triage(self, mock_run):
        """--ollama_review covers first-pass triage too, not just the blanket --ollama."""
        self._patch("OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.triage(4720)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "glm-5.3-flash:cloud")
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], triage_daemon.OLLAMA_BASE_URL)

    @patch("triage_daemon.subprocess.run")
    def test_writes_a_log_file(self, mock_run):
        """A per-issue log file is created under LOG_DIR."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.triage(4720)
        self.assertTrue((self.log_dir / "issue-4720.log").exists())

    @patch("triage_daemon.subprocess.run")
    def test_drops_the_budget_cap_when_ollama_is_configured(self, mock_run):
        """Regression test for issue #4881: --max-budget-usd's cost estimate fired
        falsely against an Ollama model, so it must not be passed at all in that mode."""
        self._patch("OLLAMA_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.triage(4720)
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--max-budget-usd", cmd)


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

    @patch("triage_daemon.subprocess.run")
    def test_adds_the_ollama_model_flag_and_env_when_configured(self, mock_run):
        """Same --ollama wiring as triage() - this flow also shells out to 'claude'."""
        self._patch("OLLAMA_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.create_pr(4720)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "glm-5.3-flash:cloud")
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], triage_daemon.OLLAMA_BASE_URL)

    @patch("triage_daemon.subprocess.run")
    def test_ollama_review_model_does_not_apply_to_pr_creation(self, mock_run):
        """--ollama_review is scoped to the review-only flows - PR creation must still
        run on the default Claude model even when --ollama_review is set."""
        self._patch("OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.create_pr(4720)
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--model", cmd)
        self.assertIsNone(mock_run.call_args.kwargs["env"])

    @patch("triage_daemon.subprocess.run")
    def test_budget_cap_still_applies_under_ollama_review(self, mock_run):
        """--ollama_review never touches PR creation - it still runs on the real
        Claude model, so its budget cap (a real spend control there) must stay."""
        self._patch("OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.create_pr(4720)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--max-budget-usd", cmd)
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "25.00")

    @patch("triage_daemon.subprocess.run")
    def test_budget_cap_dropped_under_the_blanket_ollama_flag(self, mock_run):
        """Unlike --ollama_review, --ollama covers PR creation too, so its budget
        cap - meaningless against a non-Anthropic model - is dropped here as well."""
        self._patch("OLLAMA_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.create_pr(4720)
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("--max-budget-usd", cmd)


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
            "is_actionable",
            "create_pr",
            "mark_pr_opened",
            "mark_pr_failed",
            "mark_pr_not_actionable",
        ]:
            patcher = patch.object(triage_daemon, name)
            self.patches[name] = patcher.start()
            self.addCleanup(patcher.stop)
        # Most tests don't exercise the actionable check; default it out of the way.
        self.patches["is_actionable"].return_value = True

    def test_marks_opened_when_pr_already_exists_on_entry(self):
        """A pre-existing PR (e.g. from a crashed prior run that never reached the
        label swap) converges the label to BOT_PR_OPENED instead of skipping forever."""
        self.patches["has_existing_pr"].return_value = True
        triage_daemon.process_bot_pr_issue({"number": 4720, "labels": [], "title": "Solis TOU bit refused"})
        self.patches["sync_repo"].assert_not_called()
        self.patches["create_pr"].assert_not_called()
        self.patches["mark_pr_opened"].assert_called_once_with(4720)

    def test_runs_triage_first_when_not_yet_triaged(self):
        """An untriaged issue is triaged before the PR flow runs."""
        self.patches["has_existing_pr"].side_effect = [False, True]  # entry guard, then post-hoc check
        self.patches["ensure_triaged"].return_value = False
        triage_daemon.process_bot_pr_issue({"number": 4720, "labels": [], "title": "Solis TOU bit refused"})
        self.patches["triage"].assert_called_once_with(4720)
        self.patches["create_pr"].assert_called_once_with(4720)

    def test_skips_triage_when_already_triaged(self):
        """An already-triaged issue goes straight to the PR flow."""
        self.patches["has_existing_pr"].side_effect = [False, True]
        self.patches["ensure_triaged"].return_value = True
        triage_daemon.process_bot_pr_issue({"number": 4720, "labels": [{"name": "BOT_TRIAGED"}], "title": "Solis TOU bit refused"})
        self.patches["triage"].assert_not_called()
        self.patches["create_pr"].assert_called_once_with(4720)

    def test_marks_opened_when_a_pr_exists_afterwards(self):
        """If a PR references the issue after create_pr() runs, swap to BOT_PR_OPENED."""
        self.patches["has_existing_pr"].side_effect = [False, True]
        self.patches["ensure_triaged"].return_value = True
        triage_daemon.process_bot_pr_issue({"number": 4720, "labels": [], "title": "Solis TOU bit refused"})
        self.patches["mark_pr_opened"].assert_called_once_with(4720)
        self.patches["mark_pr_failed"].assert_not_called()

    def test_marks_failed_when_no_pr_exists_afterwards(self):
        """If no PR exists after create_pr() runs (quality gate failed), swap to BOT_PR_FAILED."""
        self.patches["has_existing_pr"].side_effect = [False, False]
        self.patches["ensure_triaged"].return_value = True
        triage_daemon.process_bot_pr_issue({"number": 4720, "labels": [], "title": "Solis TOU bit refused"})
        self.patches["mark_pr_failed"].assert_called_once_with(4720)
        self.patches["mark_pr_opened"].assert_not_called()

    def test_not_actionable_after_triage_skips_create_pr(self):
        """If an inline /issue-triage classifies the ticket as a duplicate (and closes
        it), a question, or a configuration issue, the daemon must not implement it."""
        self.patches["has_existing_pr"].return_value = False
        self.patches["ensure_triaged"].return_value = False
        self.patches["is_actionable"].return_value = False
        triage_daemon.process_bot_pr_issue({"number": 4720, "labels": [], "title": "Solis TOU bit refused"})
        self.patches["triage"].assert_called_once_with(4720)
        self.patches["create_pr"].assert_not_called()
        self.patches["mark_pr_not_actionable"].assert_called_once_with(4720)
        self.patches["mark_pr_failed"].assert_not_called()

    @patch("builtins.print")
    def test_prints_the_title_and_link_before_doing_anything(self, mock_print):
        """Prints the issue's title and an openable GitHub link as soon as work starts."""
        self.patches["has_existing_pr"].return_value = True
        triage_daemon.process_bot_pr_issue({"number": 4720, "labels": [], "title": "Solis TOU bit refused"})
        printed = " ".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("Solis TOU bit refused", printed)
        self.assertIn("https://github.com/springfall2008/batpred/issues/4720", printed)


class FetchBotReviewIssuesTests(unittest.TestCase):
    """Tests for fetch_bot_review_issues(), new in the BOT_REVIEW flow."""

    @patch("triage_daemon.subprocess.run")
    def test_queries_open_issues_labelled_bot_review(self, mock_run):
        """Calls gh issue list scoped to the BOT_REVIEW label and returns the parsed issues."""
        mock_run.return_value = MagicMock(stdout=json.dumps([{"number": 3100, "labels": [{"name": "BOT_REVIEW"}], "title": "Old ticket needing review"}]))
        result = triage_daemon.fetch_bot_review_issues()
        self.assertEqual(result, [{"number": 3100, "labels": [{"name": "BOT_REVIEW"}], "title": "Old ticket needing review"}])
        args = mock_run.call_args[0][0]
        self.assertIn("--label", args)
        self.assertEqual(args[args.index("--label") + 1], "BOT_REVIEW")
        json_fields = args[args.index("--json") + 1]
        self.assertIn("title", json_fields.split(","))

    @patch("triage_daemon.subprocess.run")
    def test_requests_a_generous_limit(self, mock_run):
        """gh issue list defaults to 30 results; request enough that BOT_REVIEW issues aren't silently dropped."""
        mock_run.return_value = MagicMock(stdout=json.dumps([]))
        triage_daemon.fetch_bot_review_issues()
        args = mock_run.call_args[0][0]
        self.assertIn("--limit", args)


class RemoveReviewLabelTests(unittest.TestCase):
    """Tests for remove_review_label(), new in the BOT_REVIEW flow."""

    @patch("triage_daemon.subprocess.run")
    def test_removes_bot_review_label(self, mock_run):
        """Removes BOT_REVIEW, scoped to the configured repo."""
        triage_daemon.remove_review_label(3100)
        args = mock_run.call_args[0][0]
        self.assertEqual(args, ["gh", "issue", "edit", "3100", "--repo", "springfall2008/batpred", "--remove-label", "BOT_REVIEW"])


class MarkReviewFailedTests(unittest.TestCase):
    """Tests for mark_review_failed(), new in the BOT_REVIEW flow."""

    @patch("triage_daemon.subprocess.run")
    def test_comments_then_swaps_labels(self, mock_run):
        """Posts an explanatory comment, then swaps BOT_REVIEW for BOT_FAILED, both scoped to the configured repo."""
        triage_daemon.mark_review_failed(3100)
        calls = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(calls[0][:3], ["gh", "issue", "comment"])
        self.assertIn("--repo", calls[0])
        self.assertEqual(
            calls[1],
            [
                "gh",
                "issue",
                "edit",
                "3100",
                "--repo",
                "springfall2008/batpred",
                "--remove-label",
                "BOT_REVIEW",
                "--add-label",
                "BOT_FAILED",
            ],
        )


class TriageFollowupTests(DaemonPathsTestCase):
    """Tests for triage_followup(), new - the BOT_REVIEW follow-up flow for issues
    that already carry BOT_TRIAGED."""

    @patch("triage_daemon.subprocess.run")
    def test_invokes_claude_with_the_read_only_triage_permission_set(self, mock_run):
        """Runs /issue-triage-followup with the same triage allow/deny lists as
        first-pass triage (no commits, pushes, or PR creation)."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.triage_followup(4720)
        cmd = mock_run.call_args[0][0]
        self.assertIn("/issue-triage-followup 4720", cmd[2])
        self.assertIn(triage_daemon.ALLOWED_TOOLS, cmd)
        self.assertIn(triage_daemon.DISALLOWED_TOOLS, cmd)

    @patch("triage_daemon.subprocess.run")
    def test_raises_on_a_non_zero_exit(self, mock_run):
        """Unlike create_pr(), a follow-up failure raises - process_bot_review_issue()
        treats this the same way as a first-pass triage failure."""
        mock_run.return_value = MagicMock(returncode=1)
        with self.assertRaises(subprocess.CalledProcessError):
            triage_daemon.triage_followup(4720)

    @patch("triage_daemon.subprocess.run")
    def test_writes_a_log_file(self, mock_run):
        """A per-issue follow-up log file is created under LOG_DIR."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.triage_followup(4720)
        self.assertTrue((self.log_dir / "issue-4720-followup.log").exists())

    @patch("triage_daemon.subprocess.run")
    def test_adds_the_ollama_model_flag_and_env_when_configured(self, mock_run):
        """Same --ollama wiring as triage() - this flow also shells out to 'claude'."""
        self._patch("OLLAMA_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.triage_followup(4720)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "glm-5.3-flash:cloud")
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], triage_daemon.OLLAMA_BASE_URL)

    @patch("triage_daemon.subprocess.run")
    def test_ollama_review_model_also_applies_to_followup(self, mock_run):
        """--ollama_review covers the follow-up review too."""
        self._patch("OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.triage_followup(4720)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "glm-5.3-flash:cloud")
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], triage_daemon.OLLAMA_BASE_URL)


class ProcessBotReviewIssueTests(unittest.TestCase):
    """Tests for process_bot_review_issue(), covering all three BOT_REVIEW paths:
    first-pass triage, follow-up review, and old pre-label-ticket backfill."""

    def setUp(self):
        """Patch every collaborator process_bot_review_issue() calls."""
        self.patches = {}
        for name in [
            "is_already_triaged",
            "find_triage_comment",
            "backfill_triaged_label",
            "sync_repo",
            "reset_scratch",
            "triage",
            "triage_followup",
            "remove_review_label",
            "mark_review_failed",
        ]:
            patcher = patch.object(triage_daemon, name)
            self.patches[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def test_already_triaged_runs_a_follow_up_review(self):
        """An issue already carrying BOT_TRIAGED gets a follow-up review, not a fresh
        first-pass triage, and not the old-ticket backfill path."""
        self.patches["is_already_triaged"].return_value = True
        triage_daemon.process_bot_review_issue({"number": 3100, "labels": [{"name": "BOT_TRIAGED"}], "title": "Old ticket needing review"})
        self.patches["find_triage_comment"].assert_not_called()
        self.patches["triage"].assert_not_called()
        self.patches["sync_repo"].assert_called_once()
        self.patches["reset_scratch"].assert_called_once()
        self.patches["triage_followup"].assert_called_once_with(3100)
        self.patches["remove_review_label"].assert_called_once_with(3100)
        self.patches["mark_review_failed"].assert_not_called()

    def test_old_pre_label_ticket_backfills_without_a_follow_up(self):
        """A pre-BOT_TRIAGED-label ticket with an existing comment is backfilled and
        left alone - catching up on backlog is not the same as new information having
        arrived, so no follow-up runs."""
        self.patches["is_already_triaged"].return_value = False
        self.patches["find_triage_comment"].return_value = True
        triage_daemon.process_bot_review_issue({"number": 3100, "labels": [], "title": "Old ticket needing review"})
        self.patches["backfill_triaged_label"].assert_called_once_with(3100)
        self.patches["triage"].assert_not_called()
        self.patches["triage_followup"].assert_not_called()
        self.patches["sync_repo"].assert_not_called()
        self.patches["reset_scratch"].assert_not_called()
        self.patches["remove_review_label"].assert_called_once_with(3100)
        self.patches["mark_review_failed"].assert_not_called()

    def test_never_triaged_runs_first_pass_triage(self):
        """An issue with neither the label nor an existing comment gets a first-pass triage."""
        self.patches["is_already_triaged"].return_value = False
        self.patches["find_triage_comment"].return_value = False
        triage_daemon.process_bot_review_issue({"number": 3100, "labels": [], "title": "Old ticket needing review"})
        self.patches["sync_repo"].assert_called_once()
        self.patches["reset_scratch"].assert_called_once()
        self.patches["triage"].assert_called_once_with(3100)
        self.patches["triage_followup"].assert_not_called()
        self.patches["remove_review_label"].assert_called_once_with(3100)
        self.patches["mark_review_failed"].assert_not_called()

    def test_failed_follow_up_marks_failed_instead_of_removing_the_label(self):
        """A triage_followup() failure swaps to BOT_FAILED rather than retrying next poll."""
        self.patches["is_already_triaged"].return_value = True
        self.patches["triage_followup"].side_effect = subprocess.CalledProcessError(1, ["claude"])
        triage_daemon.process_bot_review_issue({"number": 3100, "labels": [{"name": "BOT_TRIAGED"}], "title": "Old ticket needing review"})
        self.patches["mark_review_failed"].assert_called_once_with(3100)
        self.patches["remove_review_label"].assert_not_called()

    def test_failed_first_pass_triage_marks_failed_instead_of_removing_the_label(self):
        """A triage() failure (first-pass path) also swaps to BOT_FAILED."""
        self.patches["is_already_triaged"].return_value = False
        self.patches["find_triage_comment"].return_value = False
        self.patches["triage"].side_effect = subprocess.CalledProcessError(1, ["claude"])
        triage_daemon.process_bot_review_issue({"number": 3100, "labels": [], "title": "Old ticket needing review"})
        self.patches["mark_review_failed"].assert_called_once_with(3100)
        self.patches["remove_review_label"].assert_not_called()

    @patch("builtins.print")
    def test_prints_the_title_and_link_before_doing_anything(self, mock_print):
        """Prints the issue's title and an openable GitHub link as soon as work starts."""
        self.patches["is_already_triaged"].return_value = True
        triage_daemon.process_bot_review_issue({"number": 3100, "labels": [], "title": "Old ticket needing review"})
        printed = " ".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("Old ticket needing review", printed)
        self.assertIn("https://github.com/springfall2008/batpred/issues/3100", printed)


class FetchBotReviewPrsTests(unittest.TestCase):
    """Tests for fetch_bot_review_prs(), new - the BOT_REVIEW-on-PR flow."""

    @patch("triage_daemon.subprocess.run")
    def test_queries_open_prs_labelled_bot_review(self, mock_run):
        """Calls gh pr list scoped to the BOT_REVIEW label and returns the parsed PRs."""
        mock_run.return_value = MagicMock(stdout=json.dumps([{"number": 4742, "title": "Add confirmed findings"}]))
        result = triage_daemon.fetch_bot_review_prs()
        self.assertEqual(result, [{"number": 4742, "title": "Add confirmed findings"}])
        args = mock_run.call_args[0][0]
        self.assertEqual(args[:3], ["gh", "pr", "list"])
        self.assertIn("--label", args)
        self.assertEqual(args[args.index("--label") + 1], "BOT_REVIEW")
        self.assertIn("--limit", args)


class FetchBotCleanupPrsTests(unittest.TestCase):
    """Tests for fetch_bot_cleanup_prs(), new - the BOT_CLEANUP flow."""

    @patch("triage_daemon.subprocess.run")
    def test_queries_open_prs_labelled_bot_cleanup(self, mock_run):
        """Calls gh pr list scoped to the BOT_CLEANUP label and returns the parsed PRs."""
        mock_run.return_value = MagicMock(stdout=json.dumps([{"number": 4742, "title": "Add confirmed findings"}]))
        result = triage_daemon.fetch_bot_cleanup_prs()
        self.assertEqual(result, [{"number": 4742, "title": "Add confirmed findings"}])
        args = mock_run.call_args[0][0]
        self.assertEqual(args[:3], ["gh", "pr", "list"])
        self.assertIn("--label", args)
        self.assertEqual(args[args.index("--label") + 1], "BOT_CLEANUP")
        self.assertIn("--limit", args)


class RemovePrReviewLabelTests(unittest.TestCase):
    """Tests for remove_pr_review_label(), new - the BOT_REVIEW-on-PR flow."""

    @patch("triage_daemon.subprocess.run")
    def test_removes_bot_review_label_from_the_pr(self, mock_run):
        """Removes BOT_REVIEW via gh pr edit, scoped to the configured repo."""
        triage_daemon.remove_pr_review_label(4742)
        args = mock_run.call_args[0][0]
        self.assertEqual(args, ["gh", "pr", "edit", "4742", "--repo", "springfall2008/batpred", "--remove-label", "BOT_REVIEW"])


class MarkPrReviewFailedTests(unittest.TestCase):
    """Tests for mark_pr_review_failed(), new - the BOT_REVIEW-on-PR flow."""

    @patch("triage_daemon.subprocess.run")
    def test_comments_then_swaps_labels(self, mock_run):
        """Posts an explanatory comment, then swaps BOT_REVIEW for BOT_FAILED, both on the PR."""
        triage_daemon.mark_pr_review_failed(4742)
        calls = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(calls[0][:3], ["gh", "pr", "comment"])
        self.assertIn("--repo", calls[0])
        self.assertEqual(
            calls[1],
            ["gh", "pr", "edit", "4742", "--repo", "springfall2008/batpred", "--remove-label", "BOT_REVIEW", "--add-label", "BOT_FAILED"],
        )


class ReviewPrTests(DaemonPathsTestCase):
    """Tests for review_pr(), new - runs /code-review under the comment-only permission set."""

    @patch("triage_daemon.subprocess.run")
    def test_invokes_claude_with_the_review_permission_set(self, mock_run):
        """Runs /code-review at the high effort level with --comment, under the
        review-only allow/deny lists - no write/push/commit access."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.review_pr(4742)
        cmd = mock_run.call_args[0][0]
        self.assertIn("/code-review 4742 high --comment", cmd[2])
        self.assertIn(triage_daemon.ALLOWED_TOOLS_REVIEW, cmd)
        self.assertIn(triage_daemon.DISALLOWED_TOOLS_REVIEW, cmd)

    @patch("triage_daemon.subprocess.run")
    def test_appends_the_gh_api_form_system_prompt(self, mock_run):
        """/code-review is built in, so the only place to pin its gh api command form is an
        appended system prompt - without it the posting step picks `--method POST` first and
        is denied, which is exactly how PR #4758's review came back empty."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.review_pr(4742)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--append-system-prompt", cmd)
        self.assertIn(triage_daemon.GH_API_ENDPOINT_FIRST_PROMPT, cmd[cmd.index("--append-system-prompt") + 1])

    @patch("triage_daemon.subprocess.run")
    def test_raises_on_a_non_zero_exit(self, mock_run):
        """Matches triage()/triage_followup(): a failed invocation raises."""
        mock_run.return_value = MagicMock(returncode=1)
        with self.assertRaises(subprocess.CalledProcessError):
            triage_daemon.review_pr(4742)

    @patch("triage_daemon.subprocess.run")
    def test_writes_a_log_file(self, mock_run):
        """A per-PR review log file is created under LOG_DIR."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.review_pr(4742)
        self.assertTrue((self.log_dir / "pr-4742-review.log").exists())

    @patch("triage_daemon.subprocess.run")
    def test_adds_the_ollama_model_flag_and_env_when_configured(self, mock_run):
        """Same --ollama wiring as triage() - this flow also shells out to 'claude'."""
        self._patch("OLLAMA_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.review_pr(4742)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "glm-5.3-flash:cloud")
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], triage_daemon.OLLAMA_BASE_URL)

    @patch("triage_daemon.subprocess.run")
    def test_ollama_review_model_also_applies_to_pr_review(self, mock_run):
        """--ollama_review covers PR review too - it's one of the review-only flows."""
        self._patch("OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.review_pr(4742)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "glm-5.3-flash:cloud")
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], triage_daemon.OLLAMA_BASE_URL)


class ProcessBotReviewPrTests(unittest.TestCase):
    """Tests for process_bot_review_pr(), new - the BOT_REVIEW-on-PR orchestrator."""

    def setUp(self):
        """Patch every collaborator process_bot_review_pr() calls. The activity counter
        defaults to "one more review body afterwards than before", i.e. a review that posted."""
        self.patches = {}
        for name in ["sync_repo", "reset_scratch", "review_pr", "remove_pr_review_label", "mark_pr_review_failed", "pr_review_activity_count"]:
            patcher = patch.object(triage_daemon, name)
            self.patches[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.patches["pr_review_activity_count"].side_effect = [3, 4]

    def test_reviews_then_removes_the_label(self):
        """A successful review syncs, reviews, then removes BOT_REVIEW."""
        triage_daemon.process_bot_review_pr({"number": 4742, "title": "Add confirmed findings"})
        self.patches["sync_repo"].assert_called_once()
        self.patches["reset_scratch"].assert_called_once()
        self.patches["review_pr"].assert_called_once_with(4742)
        self.patches["remove_pr_review_label"].assert_called_once_with(4742)
        self.patches["mark_pr_review_failed"].assert_not_called()

    def test_failed_review_marks_failed_instead_of_removing_the_label(self):
        """A review_pr() failure swaps to BOT_FAILED rather than retrying next poll."""
        self.patches["review_pr"].side_effect = subprocess.CalledProcessError(1, ["claude"])
        triage_daemon.process_bot_review_pr({"number": 4742, "title": "Add confirmed findings"})
        self.patches["mark_pr_review_failed"].assert_called_once()
        self.assertEqual(self.patches["mark_pr_review_failed"].call_args.args[0], 4742)
        self.patches["remove_pr_review_label"].assert_not_called()

    def test_a_review_that_posted_nothing_marks_failed_instead_of_removing_the_label(self):
        """Regression test for PR #4758: the posting step was denied by the permission rules,
        `claude -p` still exited 0, and BOT_REVIEW was cleared - so the PR ended up with no
        review, no BOT_FAILED and nothing to retry. An unchanged activity count is the only
        evidence available that nothing landed, since the exit status cannot show it."""
        self.patches["pr_review_activity_count"].side_effect = [3, 3]
        triage_daemon.process_bot_review_pr({"number": 4742, "title": "Add confirmed findings"})
        self.patches["mark_pr_review_failed"].assert_called_once()
        self.patches["remove_pr_review_label"].assert_not_called()

    def test_the_posted_nothing_comment_says_why_rather_than_just_pointing_at_the_logs(self):
        """The generic "see the logs" wording sends the maintainer to a log that, in the #4758
        case, reads like a finished review. The comment has to name the actual failure."""
        self.patches["pr_review_activity_count"].side_effect = [3, 3]
        triage_daemon.process_bot_review_pr({"number": 4742, "title": "Add confirmed findings"})
        reason = self.patches["mark_pr_review_failed"].call_args.args[1]
        self.assertIn("posted nothing", reason)

    def test_samples_the_activity_count_before_and_after_the_review(self):
        """The before-sample has to be taken ahead of review_pr(), or a PR that already had
        comments would always look like it gained one."""
        triage_daemon.process_bot_review_pr({"number": 4742, "title": "Add confirmed findings"})
        self.assertEqual(self.patches["pr_review_activity_count"].call_count, 2)
        for call in self.patches["pr_review_activity_count"].call_args_list:
            self.assertEqual(call.args, (4742,))

    def test_a_failed_review_does_not_sample_the_count_twice(self):
        """When review_pr() raises there is nothing to compare against - the failure is
        already known, so the second gh round trip is skipped."""
        self.patches["review_pr"].side_effect = subprocess.CalledProcessError(1, ["claude"])
        triage_daemon.process_bot_review_pr({"number": 4742, "title": "Add confirmed findings"})
        self.assertEqual(self.patches["pr_review_activity_count"].call_count, 1)

    @patch("builtins.print")
    def test_prints_the_title_and_link_before_doing_anything(self, mock_print):
        """Prints the PR's title and an openable GitHub link as soon as work starts."""
        triage_daemon.process_bot_review_pr({"number": 4742, "title": "Add confirmed findings"})
        printed = " ".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("Add confirmed findings", printed)
        self.assertIn("https://github.com/springfall2008/batpred/pull/4742", printed)


class RemovePrCleanupLabelTests(unittest.TestCase):
    """Tests for remove_pr_cleanup_label(), new - the BOT_CLEANUP flow."""

    @patch("triage_daemon.subprocess.run")
    def test_removes_bot_cleanup_label_from_the_pr(self, mock_run):
        """Removes BOT_CLEANUP via gh pr edit, scoped to the configured repo."""
        triage_daemon.remove_pr_cleanup_label(4742)
        args = mock_run.call_args[0][0]
        self.assertEqual(args, ["gh", "pr", "edit", "4742", "--repo", "springfall2008/batpred", "--remove-label", "BOT_CLEANUP"])


class MarkPrCleanupFailedTests(unittest.TestCase):
    """Tests for mark_pr_cleanup_failed(), new - the BOT_CLEANUP flow."""

    @patch("triage_daemon.subprocess.run")
    def test_comments_then_swaps_labels(self, mock_run):
        """Posts an explanatory comment, then swaps BOT_CLEANUP for BOT_FAILED, both on the PR."""
        triage_daemon.mark_pr_cleanup_failed(4742)
        calls = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(calls[0][:3], ["gh", "pr", "comment"])
        self.assertIn("--repo", calls[0])
        self.assertEqual(
            calls[1],
            ["gh", "pr", "edit", "4742", "--repo", "springfall2008/batpred", "--remove-label", "BOT_CLEANUP", "--add-label", "BOT_FAILED"],
        )


class CleanupPrTests(DaemonPathsTestCase):
    """Tests for cleanup_pr(), new - runs /pr-cleanup under the write-capable cleanup permission set."""

    @patch("triage_daemon.subprocess.run")
    def test_invokes_claude_with_the_cleanup_permission_set(self, mock_run):
        """Runs /pr-cleanup under the write-capable cleanup allow/deny lists."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.cleanup_pr(4742)
        cmd = mock_run.call_args[0][0]
        self.assertIn("/pr-cleanup 4742", cmd[2])
        self.assertIn(triage_daemon.ALLOWED_TOOLS_CLEANUP, cmd)
        self.assertIn(triage_daemon.DISALLOWED_TOOLS_CLEANUP, cmd)

    @patch("triage_daemon.subprocess.run")
    def test_appends_the_gh_api_form_system_prompt(self, mock_run):
        """Cleanup holds the same scoped gh api grant as the review flow, so it gets the same
        command-form guidance - it reads and replies to review threads through the REST API too."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.cleanup_pr(4742)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--append-system-prompt", cmd)
        self.assertIn(triage_daemon.GH_API_ENDPOINT_FIRST_PROMPT, cmd[cmd.index("--append-system-prompt") + 1])

    @patch("triage_daemon.subprocess.run")
    def test_raises_on_a_non_zero_exit(self, mock_run):
        """Matches triage()/triage_followup()/review_pr(): a failed invocation raises."""
        mock_run.return_value = MagicMock(returncode=1)
        with self.assertRaises(subprocess.CalledProcessError):
            triage_daemon.cleanup_pr(4742)

    @patch("triage_daemon.subprocess.run")
    def test_writes_a_log_file(self, mock_run):
        """A per-PR cleanup log file is created under LOG_DIR."""
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.cleanup_pr(4742)
        self.assertTrue((self.log_dir / "pr-4742-cleanup.log").exists())

    @patch("triage_daemon.subprocess.run")
    def test_adds_the_ollama_model_flag_and_env_when_configured(self, mock_run):
        """Same --ollama wiring as triage() - this flow also shells out to 'claude'."""
        self._patch("OLLAMA_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.cleanup_pr(4742)
        cmd = mock_run.call_args[0][0]
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "glm-5.3-flash:cloud")
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], triage_daemon.OLLAMA_BASE_URL)

    @patch("triage_daemon.subprocess.run")
    def test_ollama_review_model_does_not_apply_to_cleanup(self, mock_run):
        """--ollama_review no longer covers PR cleanup. That flow edits a maintainer's branch
        and pushes it, so it runs on Claude however the review model is set."""
        self._patch("OLLAMA_REVIEW_MODEL", "glm-5.3-flash:cloud")
        mock_run.return_value = MagicMock(returncode=0)
        triage_daemon.cleanup_pr(4742)
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("glm-5.3-flash:cloud", cmd)
        self.assertIsNone(mock_run.call_args.kwargs["env"], "cleanup must inherit the daemon environment, not the Ollama overrides")


class PushGuardHookTests(DaemonPathsTestCase):
    """The clone's pre-push hook - the only layer that sees what a ref expression resolves
    to, and therefore the only one that actually stops a push to main."""

    def _run_hook(self, remote_ref):
        """Feed the hook one ref update on stdin exactly as git does, return its exit code."""
        hook = Path(self.tmp_dir.name) / "pre-push"
        hook.write_text(triage_daemon.PUSH_GUARD_HOOK)
        hook.chmod(0o755)
        return subprocess.run(
            [str(hook)],
            input=f"refs/heads/local abc123 {remote_ref} def456\n",
            capture_output=True,
            text=True,
        )

    def test_refuses_a_push_to_main(self):
        """The 2026-09-06 incident: `git push origin HEAD:main` reaches the hook as a plain
        `refs/heads/main` remote ref, whatever it was spelled as on the command line."""
        result = self._run_hook("refs/heads/main")
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing to update main", result.stderr)

    def test_allows_a_push_to_any_other_branch(self):
        """The bot's whole job is pushing branches; only main is off limits."""
        for ref in ("refs/heads/fix/thing-4975", "refs/heads/bot/debug-journal-2026-09-07", "refs/heads/maintenance"):
            with self.subTest(ref=ref):
                self.assertEqual(self._run_hook(ref).returncode, 0)

    def test_sync_repo_installs_the_hook_before_every_flow(self):
        """`.git/hooks` is untracked, so `git clean -fd` never restores it and a hook removed
        by hand would stay removed. Rewriting it each sync is what makes it dependable."""
        (triage_daemon.CLONE_DIR / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
        with patch("triage_daemon.subprocess.run"):
            triage_daemon.sync_repo()
        hook = triage_daemon.CLONE_DIR / ".git" / "hooks" / "pre-push"
        self.assertTrue(hook.exists())
        self.assertTrue(os.access(hook, os.X_OK))

    def test_install_push_guard_overwrites_a_tampered_hook(self):
        """A run that neutered the hook must not have that survive into the next flow."""
        hooks = triage_daemon.CLONE_DIR / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "pre-push").write_text("#!/bin/sh\nexit 0\n")
        triage_daemon.install_push_guard()
        self.assertEqual((hooks / "pre-push").read_text(), triage_daemon.PUSH_GUARD_HOOK)


class PushToMainPermissionTests(unittest.TestCase):
    """No flow that can push may push to main. Deny rules are belt and braces with the hook:
    they stop the attempt earlier and fail with a legible reason."""

    MAIN_SPELLINGS = (
        "git push origin main",
        "git push -u origin main",
        "git push origin HEAD:main",
        "git push origin HEAD:refs/heads/main",
        "git push origin refs/heads/main",
        "git push --force origin main",
    )

    def _denied(self, flow, command):
        rules = getattr(triage_daemon, f"DISALLOWED_TOOLS_{flow}").split(",")
        return any(bash_rule_matches(rule, command) for rule in rules if rule.startswith("Bash("))

    def _allowed(self, flow, command):
        rules = getattr(triage_daemon, f"ALLOWED_TOOLS_{flow}").split(",")
        return any(bash_rule_matches(rule, command) for rule in rules if rule.startswith("Bash("))

    def test_no_flow_may_push_to_main(self):
        """The regression: on 2026-09-06 a cleanup run pushed an unreviewed commit to upstream
        main because the flow held an unscoped `Bash(git push*)` grant."""
        for flow in ("PR", "CLEANUP", "JOURNAL"):
            for command in self.MAIN_SPELLINGS:
                with self.subTest(flow=flow, command=command):
                    self.assertTrue(self._denied(flow, command))

    def test_no_flow_may_skip_the_pre_push_hook(self):
        """--no-verify would turn the hook off, which would leave nothing enforcing any of this."""
        for flow in ("PR", "CLEANUP", "JOURNAL"):
            for command in ("git push --no-verify", "git push --no-verify origin HEAD:main"):
                with self.subTest(flow=flow, command=command):
                    self.assertTrue(self._denied(flow, command))

    def test_the_pr_flow_can_still_push_its_own_branches(self):
        """Scoping the grant must not cost the flow its actual job."""
        for command in ("git push", "git push -u origin fix/thing-4975", "git push -u origin feat/thing-4975", "git push origin fix/thing-4975"):
            with self.subTest(command=command):
                self.assertTrue(self._allowed("PR", command) and not self._denied("PR", command))

    def test_the_cleanup_flow_gets_only_the_bare_push_its_skill_uses(self):
        """`gh pr checkout` gives the branch an upstream, so cleanup never needs to name a
        remote or a ref - and naming one is how the push to main was spelled."""
        self.assertTrue(self._allowed("CLEANUP", "git push"))
        for command in ("git push origin fix/thing-4975", "git push -u origin some-branch"):
            with self.subTest(command=command):
                self.assertFalse(self._allowed("CLEANUP", command))

    def test_the_journal_flow_keeps_its_own_branch_grant(self):
        """The narrowest grant of the three, and unaffected by the new denials."""
        self.assertTrue(self._allowed("JOURNAL", "git push -u origin bot/debug-journal-2026-09-07"))
        self.assertFalse(self._denied("JOURNAL", "git push -u origin bot/debug-journal-2026-09-07"))


class ForkHeadCleanupTests(unittest.TestCase):
    """A fork-head PR branch is not writable with this credential. Detecting that up front is
    what stops a run discovering it mid-flight and improvising a different target."""

    def test_detects_a_fork_head(self):
        """The shape that led to the 2026-09-06 push to main."""
        self.assertTrue(triage_daemon.pr_head_is_fork({"number": 4846, "headRepositoryOwner": {"login": "gcoan"}}))

    def test_a_branch_in_this_repo_is_not_a_fork(self):
        """The ordinary case must be unaffected."""
        self.assertFalse(triage_daemon.pr_head_is_fork({"number": 4968, "headRepositoryOwner": {"login": "springfall2008"}}))

    def test_missing_owner_is_not_treated_as_a_fork(self):
        """Absent data should not silently disable the cleanup flow for every PR."""
        for pr in ({"number": 1}, {"number": 2, "headRepositoryOwner": None}):
            with self.subTest(pr=pr):
                self.assertFalse(triage_daemon.pr_head_is_fork(pr))

    def test_the_query_asks_for_the_head_owner(self):
        """pr_head_is_fork() can only work if the field is actually fetched."""
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]")
            triage_daemon.fetch_bot_cleanup_prs()
            cmd = mock_run.call_args[0][0]
        self.assertIn("headRepositoryOwner", cmd[cmd.index("--json") + 1])


class ForkHeadSkipTests(unittest.TestCase):
    """A fork-head PR is skipped with an explanation rather than attempted."""

    def setUp(self):
        """Patch every collaborator process_bot_cleanup_pr() calls."""
        self.patches = {}
        for name in ["sync_repo", "reset_scratch", "cleanup_pr", "remove_pr_cleanup_label", "mark_pr_cleanup_failed", "mark_pr_cleanup_unsupported"]:
            patcher = patch.object(triage_daemon, name)
            self.patches[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_fork_head_pr_is_never_checked_out_or_cleaned(self):
        """Nothing should run against a PR whose fixes could not be pushed back anyway."""
        triage_daemon.process_bot_cleanup_pr({"number": 4846, "title": "x", "headRepositoryOwner": {"login": "gcoan"}})
        self.patches["sync_repo"].assert_not_called()
        self.patches["cleanup_pr"].assert_not_called()
        self.patches["mark_pr_cleanup_unsupported"].assert_called_once_with(4846)

    def test_the_label_is_cleared_so_it_is_not_retried_every_poll(self):
        """Left labelled, such a PR would be picked up on every cycle forever."""
        triage_daemon.process_bot_cleanup_pr({"number": 4846, "title": "x", "headRepositoryOwner": {"login": "gcoan"}})
        self.patches["remove_pr_cleanup_label"].assert_not_called()
        self.patches["mark_pr_cleanup_unsupported"].assert_called_once()


class ProcessBotCleanupPrTests(unittest.TestCase):
    """Tests for process_bot_cleanup_pr(), new - the BOT_CLEANUP orchestrator."""

    def setUp(self):
        """Patch every collaborator process_bot_cleanup_pr() calls."""
        self.patches = {}
        for name in ["sync_repo", "reset_scratch", "cleanup_pr", "remove_pr_cleanup_label", "mark_pr_cleanup_failed", "mark_pr_cleanup_unsupported"]:
            patcher = patch.object(triage_daemon, name)
            self.patches[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def test_cleans_up_then_removes_the_label(self):
        """A successful cleanup syncs, cleans up, then removes BOT_CLEANUP."""
        triage_daemon.process_bot_cleanup_pr({"number": 4742, "title": "Add confirmed findings"})
        self.patches["sync_repo"].assert_called_once()
        self.patches["reset_scratch"].assert_called_once()
        self.patches["cleanup_pr"].assert_called_once_with(4742)
        self.patches["remove_pr_cleanup_label"].assert_called_once_with(4742)
        self.patches["mark_pr_cleanup_failed"].assert_not_called()

    def test_failed_cleanup_marks_failed_instead_of_removing_the_label(self):
        """A cleanup_pr() failure swaps to BOT_FAILED rather than retrying next poll."""
        self.patches["cleanup_pr"].side_effect = subprocess.CalledProcessError(1, ["claude"])
        triage_daemon.process_bot_cleanup_pr({"number": 4742, "title": "Add confirmed findings"})
        self.patches["mark_pr_cleanup_failed"].assert_called_once_with(4742)
        self.patches["remove_pr_cleanup_label"].assert_not_called()

    @patch("builtins.print")
    def test_prints_the_title_and_link_before_doing_anything(self, mock_print):
        """Prints the PR's title and an openable GitHub link as soon as work starts."""
        triage_daemon.process_bot_cleanup_pr({"number": 4742, "title": "Add confirmed findings"})
        printed = " ".join(str(call.args[0]) for call in mock_print.call_args_list)
        self.assertIn("Add confirmed findings", printed)
        self.assertIn("https://github.com/springfall2008/batpred/pull/4742", printed)


class CommentDisclosureTests(unittest.TestCase):
    """Regression tests ensuring every comment triage_daemon.py posts directly (as
    opposed to a comment an LLM invocation composes at runtime - those get their own
    disclosure instructions in the relevant SKILL.md, or in GH_API_ENDPOINT_FIRST_PROMPT
    for /code-review, which is covered by GhApiFormPromptTests instead) opens with a
    plain "Automated ..." disclosure, so a maintainer never mistakes one for a human's."""

    @staticmethod
    def _body_of_first_comment_call(mock_run):
        """Return the --body argument of the first `gh issue/pr comment` call made,
        skipping any subsequent label-edit calls the same function also makes."""
        for call in mock_run.call_args_list:
            args = call.args[0]
            if args[:2] in (["gh", "issue"], ["gh", "pr"]) and "comment" in args:
                return args[args.index("--body") + 1]
        raise AssertionError(f"no comment call found among {mock_run.call_args_list}")

    @patch("triage_daemon.subprocess.run")
    def test_mark_pr_not_actionable_discloses(self, mock_run):
        triage_daemon.mark_pr_not_actionable(4720)
        self.assertTrue(self._body_of_first_comment_call(mock_run).startswith("Automated"))

    @patch("triage_daemon.subprocess.run")
    def test_mark_review_failed_discloses(self, mock_run):
        triage_daemon.mark_review_failed(3100)
        self.assertTrue(self._body_of_first_comment_call(mock_run).startswith("Automated"))

    @patch("triage_daemon.subprocess.run")
    def test_mark_pr_review_failed_discloses(self, mock_run):
        triage_daemon.mark_pr_review_failed(4742)
        self.assertTrue(self._body_of_first_comment_call(mock_run).startswith("Automated"))

    @patch("triage_daemon.subprocess.run")
    def test_mark_pr_cleanup_failed_discloses(self, mock_run):
        triage_daemon.mark_pr_cleanup_failed(4742)
        self.assertTrue(self._body_of_first_comment_call(mock_run).startswith("Automated"))


class JournalQueueTests(DaemonPathsTestCase):
    """The queue is where a flow parks a finding for the daily journal PR to pick up."""

    def test_queue_lives_outside_the_clone(self):
        """sync_repo() runs `git reset --hard` and `git clean -fd` on the clone before every
        flow, so anything queued inside it would be destroyed before the flush ever saw it.
        That is why every earlier attempt to have the bot maintain the journal came to
        nothing, permissions aside."""
        self.assertFalse(str(triage_daemon.QUEUE_DIR).startswith(str(triage_daemon.CLONE_DIR)))

    def test_every_flow_may_write_to_the_queue(self):
        """All five flows capture findings, so the queue's Edit grant has to be in the shared
        base list rather than added per flow."""
        rule = f"Edit({triage_daemon.QUEUE_SCOPE})"
        for name in ("ALLOWED_TOOLS", "ALLOWED_TOOLS_PR", "ALLOWED_TOOLS_REVIEW", "ALLOWED_TOOLS_CLEANUP"):
            with self.subTest(allowlist=name):
                self.assertIn(rule, getattr(triage_daemon, name).split(","))

    def test_entries_are_empty_when_the_queue_has_never_been_written(self):
        """A fresh install has no queue directory at all; that is not an error."""
        self.assertEqual(triage_daemon.journal_queue_entries(), [])

    def test_entries_are_sorted_and_only_markdown(self):
        """Sorted so the flush reads them in a stable order, and filtered so a stray download
        or editor swap file in the directory cannot be mistaken for a finding."""
        triage_daemon.QUEUE_DIR.mkdir(parents=True)
        for name in ("4931-b.md", "4900-a.md", "notes.txt"):
            (triage_daemon.QUEUE_DIR / name).write_text("x")
        self.assertEqual([p.name for p in triage_daemon.journal_queue_entries()], ["4900-a.md", "4931-b.md"])


class JournalCapturePromptTests(unittest.TestCase):
    """JOURNAL_CAPTURE_PROMPT is how a flow learns to capture at all. No skill asked for this
    before, which is why journal upkeep was self-motivated and patchy - and /code-review is a
    built-in skill whose SKILL.md we do not own, so an appended system prompt is the only
    lever that reaches every flow."""

    def test_names_the_queue_directory(self):
        """Concrete enough to act on: the agent has to be told where to write."""
        self.assertIn(str(triage_daemon.QUEUE_DIR), triage_daemon.JOURNAL_CAPTURE_PROMPT)

    def test_asks_only_for_what_was_verified(self):
        """The journal's value is that its entries are checkable. A queue full of hypotheses
        would poison it faster than leaving it stale."""
        prompt = triage_daemon.JOURNAL_CAPTURE_PROMPT
        self.assertIn("verified", prompt)
        self.assertIn("how you verified", prompt)

    def test_tells_the_agent_to_skip_when_there_is_nothing_worth_saying(self):
        """Most runs learn nothing new. Without an explicit opt-out the model writes something
        anyway, and the daily PR fills with restatements of what the journal already says."""
        self.assertIn("nothing", triage_daemon.JOURNAL_CAPTURE_PROMPT.lower())


class JournalCaptureReachesEveryFlowTests(DaemonPathsTestCase):
    """Every claude-invoking flow must carry the capture instruction."""

    def _appended_system_prompt(self, mock_run):
        """Return the --append-system-prompt value from the mocked claude invocation."""
        cmd = mock_run.call_args[0][0]
        return cmd[cmd.index("--append-system-prompt") + 1]

    def _run_flow(self, flow, *args):
        """Invoke one flow with subprocess.run mocked, returning its appended system prompt."""
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            flow(*args)
            return self._appended_system_prompt(mock_run)

    def test_every_flow_appends_the_capture_prompt(self):
        """Triage, follow-up, PR creation, PR review and PR cleanup all capture."""
        flows = [
            (triage_daemon.triage, 4931),
            (triage_daemon.triage_followup, 4931),
            (triage_daemon.create_pr, 4931),
            (triage_daemon.review_pr, 4941),
            (triage_daemon.cleanup_pr, 4941),
        ]
        for flow, arg in flows:
            with self.subTest(flow=flow.__name__):
                self.assertIn(triage_daemon.JOURNAL_CAPTURE_PROMPT, self._run_flow(flow, arg))

    def test_review_and_cleanup_keep_the_gh_api_steer_as_well(self):
        """These two already carried GH_API_ENDPOINT_FIRST_PROMPT. Appending the capture text
        must add to it, not replace it - the #4758 fix depends on that steer surviving."""
        for flow, arg in ((triage_daemon.review_pr, 4941), (triage_daemon.cleanup_pr, 4941)):
            with self.subTest(flow=flow.__name__):
                appended = self._run_flow(flow, arg)
                self.assertIn(triage_daemon.GH_API_ENDPOINT_FIRST_PROMPT, appended)
                self.assertIn(triage_daemon.JOURNAL_CAPTURE_PROMPT, appended)

    def test_only_one_append_system_prompt_flag_is_passed(self):
        """Two --append-system-prompt flags is not a documented way to pass two prompts, so
        the parts are joined into a single value instead."""
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            triage_daemon.review_pr(4941)
            self.assertEqual(mock_run.call_args[0][0].count("--append-system-prompt"), 1)


class JournalFlushGateTests(DaemonPathsTestCase):
    """The flush runs at most once a day, and only when something is waiting."""

    def _queue(self, count):
        """Put `count` candidate files in the queue directory."""
        triage_daemon.QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        for i in range(count):
            (triage_daemon.QUEUE_DIR / f"{i}.md").write_text("finding")

    def test_no_flush_when_the_queue_is_empty(self):
        """A quiet day opens no PR at all, rather than an empty one."""
        self.assertFalse(triage_daemon.should_flush_journal({}, "2026-09-05"))

    def test_flush_when_something_is_queued_and_today_has_not_run(self):
        self._queue(1)
        self.assertTrue(triage_daemon.should_flush_journal({}, "2026-09-05"))

    def test_no_second_flush_on_the_same_day(self):
        """The daemon polls every 5 minutes; without the date gate a non-empty queue would
        open a PR on every poll."""
        self._queue(1)
        state = {"last_journal_flush": "2026-09-05"}
        self.assertFalse(triage_daemon.should_flush_journal(state, "2026-09-05"))

    def test_flushes_again_the_next_day(self):
        self._queue(1)
        state = {"last_journal_flush": "2026-09-04"}
        self.assertTrue(triage_daemon.should_flush_journal(state, "2026-09-05"))

    def test_a_single_finding_is_enough(self):
        """One verified finding is worth a PR - the daily gate already bounds the review
        burden, so there is no minimum batch size on top of it."""
        self._queue(1)
        self.assertTrue(triage_daemon.should_flush_journal({}, "2026-09-05"))


class JournalPermissionTests(unittest.TestCase):
    """The flush flow can push and open a PR, so its blast radius is deliberately the
    narrowest of any flow: two files, and no other repo write."""

    def _allowed(self):
        return triage_daemon.ALLOWED_TOOLS_JOURNAL.split(",")

    def test_does_not_grant_clone_wide_edit(self):
        """Every other flow may edit anywhere in the clone. This one may not - it is the only
        flow that can push, so a prompt-injected edit to apps/predbat would land on a branch."""
        self.assertNotIn(f"Edit({triage_daemon.EDIT_SCOPE})", self._allowed())

    def test_grants_exactly_the_journal_and_the_dictionary(self):
        """cspell is a pre-commit hook and a journal entry naming a new vendor term fails it,
        so the dictionary has to be writable too - and nothing else does."""
        edits = sorted(rule for rule in self._allowed() if rule.startswith("Edit("))
        self.assertEqual(edits, sorted([f"Edit({triage_daemon.JOURNAL_SCOPE})", f"Edit({triage_daemon.DICTIONARY_SCOPE})"]))

    def test_the_journal_lives_outside_the_dot_claude_directory(self):
        """Claude Code refuses the Edit and Write tools anywhere under `.claude/`, and no
        allowlist entry overrides it - not an exact file path, not a clone-wide glob. While
        the journal lived at `.claude/skills/issue-triage/references/debug-journal.md` every
        flush verified its candidates and then found it could not write a byte, so the file
        went unmaintained from 2026-09-05 until the move. Reads were never affected, which is
        why the rest of the bot looked healthy."""
        self.assertFalse(triage_daemon.JOURNAL_RELPATH.startswith(".claude/"))
        self.assertIn(f"Edit({triage_daemon.JOURNAL_SCOPE})", self._allowed())

    def test_cannot_write_the_queue_it_reads(self):
        """The flush consumes candidates; it never needs to author one. Reading them comes
        from --add-dir, not from an Edit grant."""
        self.assertNotIn(f"Edit({triage_daemon.QUEUE_SCOPE})", self._allowed())

    def test_can_commit_push_and_open_a_pr(self):
        """The whole point of the flow: land the entries as a PR for a human to merge."""
        for command in ("git add -A", "git commit -m x", "git push origin bot/debug-journal-2026-09-05", "gh pr create --draft"):
            with self.subTest(command=command):
                self.assertTrue(any(bash_rule_matches(rule, command) for rule in self._allowed() if rule.startswith("Bash(")))

    def test_cannot_merge_its_own_pr(self):
        """A human merge is the review gate. Losing it would make the bot the only reviewer of
        its own edits to the file every other flow trusts."""
        denied = triage_daemon.DISALLOWED_TOOLS_JOURNAL.split(",")
        self.assertTrue(any(bash_rule_matches(rule, "gh pr merge 5000") for rule in denied if rule.startswith("Bash(")))

    def test_still_blocks_force_push_variants(self):
        """Same defence in depth as the PR flow: it can push, so it must not rewrite history."""
        denied = triage_daemon.DISALLOWED_TOOLS_JOURNAL.split(",")
        for command in ("git push --force origin main", "git push origin main -f", "git push --force-with-lease"):
            with self.subTest(command=command):
                self.assertTrue(any(bash_rule_matches(rule, command) for rule in denied if rule.startswith("Bash(")))

    def test_does_not_inherit_the_broad_gh_grant(self):
        """Same reasoning as the review and cleanup flows: a catch-all would silently re-widen
        every scoped carve-out below it."""
        self.assertNotIn("Bash(gh *)", self._allowed())

    def test_does_not_grant_a_direct_push_to_main(self):
        """main is protection-gated and the design is explicitly PR-and-human-merge, so the
        allowlist names the bot branch prefix rather than any ref."""
        self.assertFalse(any(bash_rule_matches(rule, "git push origin main") for rule in self._allowed() if rule.startswith("Bash(git push")))


class JournalFlushInvocationTests(DaemonPathsTestCase):
    """What the flush actually runs."""

    def test_invokes_the_journal_update_skill_under_its_own_permission_set(self):
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=OPENED_PR_JSON)
            triage_daemon.flush_journal("2026-09-07")
            cmd = mock_run.call_args_list[0][0][0]
        self.assertIn("/journal-update", cmd[cmd.index("-p") + 1])
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], triage_daemon.ALLOWED_TOOLS_JOURNAL)
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], triage_daemon.DISALLOWED_TOOLS_JOURNAL)

    def test_tells_the_skill_where_the_queue_is(self):
        """The queue lives outside the clone, so the path has to be passed in and added to the
        session's directory scope for Read/Grep."""
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=OPENED_PR_JSON)
            triage_daemon.flush_journal("2026-09-07")
            cmd = mock_run.call_args_list[0][0][0]
        self.assertIn(str(triage_daemon.QUEUE_DIR), cmd[cmd.index("-p") + 1])
        self.assertIn(str(triage_daemon.QUEUE_DIR), cmd[cmd.index("--add-dir") + 1 :])


class JournalQueueArchiveTests(DaemonPathsTestCase):
    """Consumed candidates are moved aside, not deleted."""

    def _queue(self, *names):
        triage_daemon.QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        for name in names:
            (triage_daemon.QUEUE_DIR / name).write_text("finding")
        return triage_daemon.journal_queue_entries()

    def test_a_successful_flush_empties_the_queue(self):
        """Otherwise the next day folds the same findings in again."""
        self._queue("4931-a.md")
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=OPENED_PR_JSON)
            triage_daemon.flush_journal("2026-09-07")
        self.assertEqual(triage_daemon.journal_queue_entries(), [])

    def test_a_failed_flush_leaves_the_queue_intact(self):
        """A finding must survive a flush that never opened a PR - the daemon has already
        stamped the date, so tomorrow's run is its next chance."""
        self._queue("4931-a.md")
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            triage_daemon.flush_journal("2026-09-07")
        self.assertEqual([p.name for p in triage_daemon.journal_queue_entries()], ["4931-a.md"])

    def test_consumed_findings_are_kept_not_deleted(self):
        """A candidate the flush decided to drop is still evidence of what was seen."""
        self._queue("4931-a.md")
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=OPENED_PR_JSON)
            triage_daemon.flush_journal("2026-09-07")
        self.assertTrue((triage_daemon.QUEUE_DIR / "processed" / "4931-a.md").exists())

    def test_a_blocked_flush_that_exits_zero_leaves_the_queue_intact(self):
        """The regression that stalled the journal on 2026-09-07. `claude -p` denied the
        permissions it needed, explained itself and exited 0, so the old exit-code check
        archived three verified candidates having landed nothing - and the non-recursive
        queue glob meant they were never offered to a later flush."""
        self._queue("4965-a.md", "4967-b.md", "4973-c.md")
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]")
            triage_daemon.flush_journal("2026-09-07")
        self.assertEqual([p.name for p in triage_daemon.journal_queue_entries()], ["4965-a.md", "4967-b.md", "4973-c.md"])
        self.assertFalse((triage_daemon.QUEUE_DIR / "processed").exists())

    def test_an_unreachable_github_does_not_archive(self):
        """A failed lookup must not be read as "no PR needed". Leaving the queue costs one
        re-verify tomorrow; archiving on a failed check loses the finding for good."""
        self._queue("4931-a.md")
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=1, stdout="")]
            triage_daemon.flush_journal("2026-09-07")
        self.assertEqual([p.name for p in triage_daemon.journal_queue_entries()], ["4931-a.md"])

    def test_the_pr_is_looked_up_by_todays_journal_branch(self):
        """The branch name is the only link between the flush and the PR it opened."""
        with patch("triage_daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=OPENED_PR_JSON)
            triage_daemon.journal_pr_opened("2026-09-07")
            cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--head") + 1], "bot/debug-journal-2026-09-07")
        self.assertEqual(cmd[cmd.index("--state") + 1], "all")

    def test_archived_findings_are_not_queued_again(self):
        """journal_queue_entries() must not recurse into the archive."""
        self._queue("4931-a.md")
        triage_daemon.archive_journal_queue(triage_daemon.journal_queue_entries())
        self.assertFalse(triage_daemon.should_flush_journal({}, "2026-09-06"))


if __name__ == "__main__":
    unittest.main()
