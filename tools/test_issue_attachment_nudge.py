#!/usr/bin/env python3
"""Tests for tools/issue_attachment_nudge.py, the decision behind the issue attachment nudge workflow.

Run directly with `python3 tools/test_issue_attachment_nudge.py`; nothing here touches GitHub.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import issue_attachment_nudge as nudge  # noqa: E402

BUG_TEMPLATE = (REPO / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").read_text(encoding="utf-8").split("---", 2)[2]
FEATURE_TEMPLATE = (REPO / ".github" / "ISSUE_TEMPLATE" / "feature_request.md").read_text(encoding="utf-8").split("---", 2)[2]

FILE_LINK = "https://github.com/user-attachments/files/23456789/predbat.log"
OLD_FILE_LINK = "https://github.com/springfall2008/batpred/files/1234567/predbat_debug.yaml.txt"
IMAGE_LINK = "https://github.com/user-attachments/assets/0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
LOG_LINES = "\n".join(
    [
        "2026-11-12 18:00:01.123456: Plan updated",
        "2026-11-12 18:00:02.123456: Warn: Inverter 0 did not accept the charge rate",
        "2026-11-12 18:00:03.123456: Error: Failed to write register",
    ]
)


def issue(body, association="NONE", user_type="User"):
    """Build a minimal issue dict as an `issues` event carries it."""
    return {"body": body, "author_association": association, "user": {"type": user_type}}


class TestDecide(unittest.TestCase):
    """The nudge/skip decision."""

    def assert_nudge(self, body, reason=None):
        """Assert the body is nudged, optionally for the given reason."""
        result, why = nudge.decide(issue(body))
        self.assertTrue(result, "expected a nudge for: {!r} ({})".format(body, why))
        if reason:
            self.assertEqual(why, reason)

    def assert_skip(self, body, **kwargs):
        """Assert the body is not nudged."""
        result, why = nudge.decide(issue(body, **kwargs))
        self.assertFalse(result, "expected no nudge for: {!r}".format(body))
        return why

    def test_offers_are_nudged(self):
        """Phrasings of offering the files rather than attaching them."""
        for body in [
            "The plan exports at 4pm for no reason. Logs available on request.",
            "Full debug yaml can be provided if needed.",
            "I'm happy to provide logs.",
            "I can attach the debug yaml if that would help.",
            "Happy to share the full log file",
            "Let me know if you need the logs.",
            "Log files available upon request",
            "On request I can send the debug file",
            "I could upload the predbat log",
            "I can provide the predbat.log and predbat_debug.yaml if needed.",
            "I can provide predbat.log",
            "Let me know if you need the predbat.log",
            "Happy to attach the predbat_debug.yaml and predbat.log",
            "I'm happy to provide the predbat_debug.yaml",
            "Can supply predbat_debug.yaml on request",
            "predbat_debug.yaml.txt can be provided if needed",
            "I can provide the full predbat.log, the debug yaml and screenshots of the plan if needed.",
            "Logs are available if you need them",
            "Debug yaml and logs available - just ask",
            "I would be happy to provide the predbat.log",
        ]:
            with self.subTest(body=body):
                self.assert_nudge(body, nudge.REASON_OFFER)

    def test_non_offers_are_not_nudged(self):
        """Sentences that name the files without offering them, or that say they can't be had."""
        for body in [
            "I am not able to provide logs as Predbat crashed",
            "I can't attach the debug yaml, the download link does nothing.",
            "Previously I could attach the debug yaml, but now the download link fails",
            "The inverter will send the logs to the cloud every 5 minutes.",
            "my profile can be provided if needed",
            "The catalog was provided when needed",
            "I can upload screenshots of the files tab",
            "I turned on debug mode and will share screenshots.",
            "When will Predbat download the debug file? It is only uploaded if requested.",
        ]:
            with self.subTest(body=body):
                self.assert_skip(body)

    def test_feature_requests_are_not_nudged(self):
        """A feature request needs no evidence, even one that mentions the debug file."""
        self.assert_skip(FEATURE_TEMPLATE + "\nThe debug file could be provided on request via an HA service.")
        result, why = nudge.decide({"title": "Feature request: send logs on request", "body": "The debug yaml could be shared on request.", "user": {"type": "User"}})
        self.assertFalse(result, why)

    def test_offer_in_filled_bug_template_is_nudged(self):
        """An offer typed into the bug template's Log file section."""
        body = BUG_TEMPLATE.replace("xxxx", "v8.31.2") + "\nLogs available on request.\n"
        self.assert_nudge(body, nudge.REASON_OFFER)

    def test_pasted_log_lines_are_nudged(self):
        """Quoting the log in place of attaching it."""
        self.assert_nudge("The inverter ignores Predbat:\n```\n" + LOG_LINES + "\n```", nudge.REASON_PASTED)

    def test_one_or_two_log_lines_are_not_enough(self):
        """A single quoted error line, such as a traceback's last line, is not quoting the log in place of it."""
        self.assert_skip("I see this error:\n" + LOG_LINES.splitlines()[2])

    def test_attachment_skips(self):
        """Any attached file means the reporter has got the idea, even with an offer of more."""
        for link in [FILE_LINK, OLD_FILE_LINK]:
            with self.subTest(link=link):
                self.assertEqual(self.assert_skip("Logs available on request. Debug: " + link), "already has a file attached")

    def test_screenshot_alone_does_not_count_as_attachment(self):
        """Images are not the files."""
        self.assert_nudge("Screenshot: " + IMAGE_LINK + "\nLogs available on request.")

    def test_untouched_templates_are_not_nudged(self):
        """The templates' own text, including the HTML note to AI assistants, must not trigger a nudge."""
        self.assert_skip(BUG_TEMPLATE)
        self.assert_skip(FEATURE_TEMPLATE)

    def test_template_note_without_its_comment_delimiters(self):
        """A body that pastes the AI-assistant note as plain text must not read as an offer."""
        self.assert_skip(BUG_TEMPLATE.replace("<!--", "").replace("-->", ""))

    def test_unclosed_html_comment_is_ignored(self):
        """An unclosed comment runs to the end of the body, as GitHub renders it."""
        self.assert_skip("The plan looks odd.\n<!-- logs available on request")

    def test_html_comment_is_ignored(self):
        """An offer inside an HTML comment is template guidance, not the reporter."""
        self.assert_skip("<!-- do not write 'logs available on request' -->\nThe plan looks odd.")

    def test_plain_reports_are_left_to_triage(self):
        """No offer and no pasted log: the triage bot decides whether files are needed."""
        for body in [
            "My plan doesn't look right",
            "Feature request: support for a new tariff.",
            "The docs say the default is 10 but it's 5.",
            "I've attached nothing, is this a config problem?",
        ]:
            with self.subTest(body=body):
                self.assert_skip(body)

    def test_trusted_authors_and_bots_skip(self):
        """Maintainers, collaborators and bots are not nudged."""
        body = "Logs available on request."
        for association in ["OWNER", "MEMBER", "COLLABORATOR"]:
            with self.subTest(association=association):
                self.assert_skip(body, association=association)
        self.assert_skip(body, user_type="Bot")
        self.assert_nudge(body)  # a CONTRIBUTOR or first-timer is

    def test_empty_body(self):
        """An issue with no body at all."""
        self.assert_skip(None)


class TestMain(unittest.TestCase):
    """The command-line entry point the workflow runs."""

    def run_script(self, body):
        """Run the script on an event with the given body; return (stdout, comment or None)."""
        with tempfile.TemporaryDirectory() as tmp:
            event_path = os.path.join(tmp, "event.json")
            out_path = os.path.join(tmp, "nudge.md")
            with open(event_path, "w", encoding="utf-8") as fh:
                json.dump({"action": "opened", "issue": issue(body)}, fh)
            result = subprocess.run([sys.executable, "-I", str(TOOLS / "issue_attachment_nudge.py"), event_path, out_path], capture_output=True, text=True, check=True)
            comment = Path(out_path).read_text(encoding="utf-8") if os.path.exists(out_path) else None
        return result.stdout.strip(), comment

    def test_nudge_writes_comment(self):
        """A nudge prints 'nudge' and writes a comment that starts with the marker."""
        stdout, comment = self.run_script("Happy to provide logs.")
        self.assertEqual(stdout, "nudge")
        self.assertTrue(comment.startswith(nudge.MARKER))
        self.assertIn(nudge.REASON_OFFER, comment)
        self.assertIn("predbat_debug.yaml.txt", comment)
        self.assertIn(nudge.ASK[nudge.REASON_OFFER], comment)

    def test_skip_writes_nothing(self):
        """A skip prints the reason and writes no comment."""
        stdout, comment = self.run_script("My plan doesn't look right")
        self.assertTrue(stdout.startswith("skip: "))
        self.assertIsNone(comment)


if __name__ == "__main__":
    unittest.main()
