#!/usr/bin/env python3
"""Decide whether a newly opened issue needs a nudge to attach its log and debug files.

Run by .github/workflows/issue-attachment-nudge.yml on `issues: opened`. Many reports, often
drafted with an AI assistant's help, offer the files ("logs available on request") or paste a
summary of them instead of attaching them, which costs a round trip before triage can start.
This posts the request straight away, while the reporter is still at the keyboard.

It nudges only when no file is attached and the body either offers the files or pastes log
lines in place of them. It deliberately does not nudge every report without attachments: the
triage bot decides that case with more context. Nor does it make reporting any easier: it asks
for more, not less.

Usage: issue_attachment_nudge.py <event.json> <comment-out.md>
Writes the comment to <comment-out.md> and prints "nudge" when one should be posted, else prints
"skip: <reason>". Standard library only, as it runs on a bare GitHub runner.
"""

import json
import re
import sys

# Starts every nudge, so the triage bot (and a re-run) can recognise one
MARKER = "<!-- predbat-attachment-nudge -->"

# Authors who know the drill; their issues are often notes for maintainers
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

# A file uploaded to GitHub: new-style user-attachments/files links, or the older per-repo /files/ links.
# Images go to user-attachments/assets, so a screenshot alone does not count as a file
ATTACHMENT_RE = re.compile(r"https://github\.com/(user-attachments/files/|[\w.-]+/[\w.-]+/files/)\d+", re.IGNORECASE)

# Offering the files instead of attaching them
_FILE_WORDS = r"(?:logs?|log\s+files?|debug(?:\s+(?:yaml|file|files|dump))?|yaml|files?|dumps?)"
OFFER_RES = [
    # "logs available on request", "debug file can be provided if needed", "full log available upon request"
    re.compile(_FILE_WORDS + r"\b[^.\n]{0,40}?\b(?:available|provided|attached|shared|supplied|sent|uploaded)\b[^.\n]{0,25}?\b(?:on|upon|if|when)\s+(?:request|requested|needed|required|necessary|wanted|helpful|useful)", re.IGNORECASE),
    # "happy to provide logs", "can attach the debug yaml", "I can share the full log if needed"
    re.compile(r"\b(?:happy|glad|able|willing|can|could|will)\s+(?:to\s+)?(?:provide|attach|share|send|supply|upload)\b[^.\n]{0,30}?\b" + _FILE_WORDS + r"\b", re.IGNORECASE),
    # "available on request" / "upon request" / "if needed" next to a file word in either order
    re.compile(r"\b(?:on|upon)\s+request\b[^.\n]{0,40}?\b" + _FILE_WORDS + r"\b", re.IGNORECASE),
    # "let me know if you need the logs"
    re.compile(r"\blet\s+me\s+know\s+if\s+(?:you\s+)?(?:need|want|would\s+like)\b[^.\n]{0,30}?\b" + _FILE_WORDS + r"\b", re.IGNORECASE),
]

# A line pasted from predbat.log: "2026-11-13 09:25:50.445865: Warn: ..."
LOG_LINE_RE = re.compile(r"^\W*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?:\s*(?:Info|Warn|Error)\b", re.MULTILINE)

# Pasted log lines needed before a report counts as quoting the log in place of the file
LOG_LINES_THRESHOLD = 3

COMMENT = """{marker}
This is an automated message. Thanks for the report. {reason}

**Please attach the files themselves to this issue before it is looked at.** A summary, a table or a few quoted lines is a useful addition, but the files are needed to check them and to replay your plan.

- **If the problem is happening now, or will happen again:** download `predbat_debug.yaml` and `predbat.log` from the **Debug** panel on the **Dash** tab of Predbat's web interface while it is happening. The debug file arrives named `predbat_debug.yaml.txt`; attach it as-is. Use a web browser: the HA Companion app cannot save files.
- **If it is over:** open Predbat's web interface → **Plan** → **History**, and download the debug snapshot from the **Debug** column next to the time it happened. Only about the last 48 hours are kept, so please grab it soon. Attach a `predbat.log` covering that time too, if you still have one.
- **Screenshots help too, alongside the files:** Predbat's plan (web interface → **Plan**), showing as much of it as you can, and the battery chart around the time of the problem.
- To attach a file, drag it into a comment box on this issue (or edit the issue and drop it there).

If you are using an AI assistant to write this report, please ask it to tell you which files to download rather than offering them. See [debug history](https://springfall2008.github.io/batpred/customisation/#debug-history) for more on the snapshots.
"""

REASON_OFFER = "It offers the log or debug files, but they are not attached yet."
REASON_PASTED = "It quotes lines from the log, but the log file itself is not attached."


def strip_html_comments(body):
    """Remove HTML comments, which hold template guidance rather than anything the reporter wrote."""
    return re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)


def decide(issue):
    """Return (nudge, reason) for an issue dict from a GitHub `issues` event."""
    if (issue.get("user") or {}).get("type") == "Bot":
        return False, "opened by a bot"
    if issue.get("author_association") in TRUSTED_ASSOCIATIONS:
        return False, "opened by a maintainer or collaborator"
    if issue.get("pull_request"):
        return False, "a pull request"
    body = strip_html_comments(issue.get("body") or "")
    if ATTACHMENT_RE.search(body):
        return False, "already has a file attached"
    if any(rx.search(body) for rx in OFFER_RES):
        return True, REASON_OFFER
    if len(LOG_LINE_RE.findall(body)) >= LOG_LINES_THRESHOLD:
        return True, REASON_PASTED
    return False, "no offer of files and no pasted log lines"


def render(reason):
    """Return the comment text for a nudge with the given reason."""
    return COMMENT.format(marker=MARKER, reason=reason)


def main(argv):
    """Read the event, write the comment if one is due, and print the decision."""
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        event = json.load(fh)
    nudge, reason = decide(event.get("issue") or {})
    if not nudge:
        print("skip: " + reason)
        return 0
    with open(argv[2], "w", encoding="utf-8") as fh:
        fh.write(render(reason))
    print("nudge")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
