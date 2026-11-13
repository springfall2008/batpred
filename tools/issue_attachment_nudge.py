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

# Starts every nudge, so the triage bot, the triage daemon (ATTACHMENT_NUDGE_MARKER) and a re-run
# of the workflow can recognise one
MARKER = "<!-- predbat-attachment-nudge -->"

# Authors who know the drill; their issues are often notes for maintainers
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

# A file uploaded to GitHub: new-style user-attachments/files links, or the older per-repo /files/ links.
# Images go to user-attachments/assets, so a screenshot alone does not count as a file
ATTACHMENT_RE = re.compile(r"https://github\.com/(user-attachments/files/|[\w.-]+/[\w.-]+/files/)\d+", re.IGNORECASE)

# A word naming the log or debug files. Letter-only boundaries, so "predbat.log" and "predbat_debug"
# count but "catalog" and "dialog" do not; "debug mode" is a switch, not a file
FILE_WORD_RE = re.compile(r"(?<![A-Za-z])(?:logs?|log\s+files?|debug(?!\s+mode)(?:\s+(?:yaml|files?|dump))?|yaml|dumps?)(?![A-Za-z])", re.IGNORECASE)

# Ways of offering the files rather than attaching them. Each is checked within one sentence
# that also names a file (FILE_WORD_RE)
OFFER_RES = [
    # The reporter offering: "I can provide predbat.log", "happy to attach the debug yaml", "we'll send the logs"
    re.compile(r"(?<![A-Za-z])(?:I|I'm|I'd|I'll|we|we're|we'd|we'll|happy|glad|willing)\b[^\n]{0,30}?\b(?:provide|attach|share|send|supply|upload)\b", re.IGNORECASE),
    # Offered on a condition: "logs available on request", "predbat_debug.yaml can be provided if needed",
    # "logs are available if you need them", "debug yaml and logs available - just ask"
    re.compile(r"\b(?:(?:on|upon)\s+(?:request|demand)|if\s+(?:you\s+)?(?:need|want|require|requested|needed|required|necessary|helpful|useful|wanted)|when\s+(?:needed|requested|required)|just\s+ask)\b", re.IGNORECASE),
    # "let me know if you need the logs"
    re.compile(r"\blet\s+(?:me|us)\s+know\s+if\b", re.IGNORECASE),
]

# A sentence with any of these is not an offer: "I am not able to provide logs", "Previously I
# could attach the debug yaml, but now the download fails"
NOT_AN_OFFER_RE = re.compile(r"\b(?:not|never|unable|cannot|can't|couldn't|won't|don't|doesn't|isn't|wasn't|previously|used\s+to|no\s+longer|anymore)\b", re.IGNORECASE)

# Sentences end at ! or ? or a full stop followed by a space, or at a line break. The full stop
# in "predbat.log" is followed by a letter, so it does not end one
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n")

# A feature request needs no evidence: the feature template's heading, or a title that says so
FEATURE_REQUEST_RE = re.compile(r"Is your feature request related to a problem")
FEATURE_TITLE_RE = re.compile(r"^\W*(?:feature(?:\s+request)?|enhancement|FR)\b", re.IGNORECASE)

# A line pasted from predbat.log, which starts every line "2026-11-13 09:25:50.445865: ", with or
# without an Info/Warn/Error level after it
LOG_LINE_RE = re.compile(r"^\W*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?:\s", re.MULTILINE)

# Pasted log lines needed before a report counts as quoting the log in place of the file
LOG_LINES_THRESHOLD = 3

COMMENT = """{marker}
This is an automated message. Thanks for the report. {reason}

{ask} A summary, a table or a few quoted lines is a useful addition, but the files are needed to check them and to replay your plan.

- **If the problem is happening now, or will happen again:** download `predbat_debug.yaml` and `predbat.log` from the **Debug** panel on the **Dash** tab of Predbat's web interface while it is happening. The debug file arrives named `predbat_debug.yaml.txt`; attach it as-is.
- **If it is over:** open Predbat's web interface → **Plan** → **History**, and download the debug snapshot from the **Debug** column, on the nearest row with a link before the problem started (by default a snapshot is kept every 3 hours). Only about the last 48 hours are kept, so please grab it soon. Attach a `predbat.log` covering that time too, if you still have one.
- **On the HA Companion app**, which can't save files, take them from the `debug/` folder under your Predbat config directory instead, with the File editor or Samba add-on.
- **Screenshots help too, alongside the files:** Predbat's plan (web interface → **Plan**), showing as much of it as you can, and the battery chart around the time of the problem.
- To attach a file, drag it into a comment box on this issue (or edit the issue and drop it there).

If you are using an AI assistant to write this report, please ask it to tell you which files to download rather than offering them. See [debug history](https://springfall2008.github.io/batpred/customisation/#debug-history) for more on the snapshots.
"""

REASON_OFFER = "It offers the log or debug files, but they are not attached yet."
REASON_PASTED = "It quotes lines from the log, but the log file itself is not attached."
ASK = {
    REASON_OFFER: "**Please attach the files themselves to this issue before it is looked at.**",
    REASON_PASTED: "**The files themselves will very likely be needed, so please attach them to this issue.**",
}


def strip_html_comments(body):
    """Remove HTML comments, which hold template guidance rather than anything the reporter wrote.

    An unclosed comment runs to the end of the body, as it does when GitHub renders it.
    """
    return re.sub(r"<!--.*?(?:-->|\Z)", "", body, flags=re.DOTALL)


def offers_files(body):
    """Return True if any one sentence of the body names the files and offers them without attaching them."""
    for sentence in SENTENCE_SPLIT_RE.split(body):
        if FILE_WORD_RE.search(sentence) and not NOT_AN_OFFER_RE.search(sentence) and any(rx.search(sentence) for rx in OFFER_RES):
            return True
    return False


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
    if FEATURE_REQUEST_RE.search(body) or FEATURE_TITLE_RE.search(issue.get("title") or ""):
        return False, "a feature request"
    if offers_files(body):
        return True, REASON_OFFER
    if len(LOG_LINE_RE.findall(body)) >= LOG_LINES_THRESHOLD:
        return True, REASON_PASTED
    return False, "no offer of files and no pasted log lines"


def render(reason):
    """Return the comment text for a nudge with the given reason."""
    return COMMENT.format(marker=MARKER, reason=reason, ask=ASK[reason])


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
