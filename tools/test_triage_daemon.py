#!/usr/bin/env python3
"""Unit tests for tools/triage_daemon.py.

Runs standalone, not through coverage/run_all: the daemon has no dependency on
apps/predbat, so folding these into TEST_REGISTRY would be a layering violation.
Run directly with `python3 tools/test_triage_daemon.py`. Every gh/git/claude call is
mocked - nothing here touches a real repo, GitHub, or Claude Code session.
"""

import json
import subprocess
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


class IssueUrlTests(unittest.TestCase):
    """Tests for issue_url(), new - used to print an openable link when work starts."""

    def test_builds_the_github_issue_url(self):
        """Returns the standard GitHub issue URL for the configured repo."""
        self.assertEqual(triage_daemon.issue_url(4720), "https://github.com/springfall2008/batpred/issues/4720")


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
        triage_daemon.mark_pr_opened(4720)
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
                "BOT_PR_OPENED",
            ],
        )

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

    def test_pr_disallowed_tools_still_blocks_force_push_variants(self):
        """Even though the PR flow can push, force-push stays denied - defense in depth
        against a prompt-injected instruction attempting to rewrite history."""
        pr_denied = triage_daemon.DISALLOWED_TOOLS_PR.split(",")
        self.assertTrue(any("force" in entry for entry in pr_denied), pr_denied)
        self.assertTrue(any("-f" in entry for entry in pr_denied), pr_denied)


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


if __name__ == "__main__":
    unittest.main()
