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


if __name__ == "__main__":
    unittest.main()
