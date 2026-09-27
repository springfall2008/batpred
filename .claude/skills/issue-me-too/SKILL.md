---
name: issue-me-too
description: Check whether a "me too" comment on an already-triaged batpred GitHub issue is clearly a different problem, and if so ask its author to open their own issue and record that the comment is unrelated.
allowed-tools: You can view files in this repo, download and grep issue attachments, and access the issue in github with 'gh'. Do not go outside this sandbox or push any changes back to git.
---

# Issue Me-Too Check

You are checking one comment on an already-triaged GitHub issue on `springfall2008/batpred`. Someone other than the reporter has posted what they believe is the same problem. Start from trusting them: it usually is the same problem, and their report and files then help the original. The exception is a post that is clearly a different problem. Folded into the thread, it would muddy the analysis of the original, and the commenter's own problem would never get triaged. Your job is to spot that exception, ask the commenter to file their own issue, and record that the post is unrelated so later reviews leave it out.

This is **not** an investigation. Do not look for a root cause, read the code for one, run tests, or offer any theory or workaround, for either problem.

Arguments: `<issue-number> comment=<comment-url> [scratch=<dir>]`. If no `scratch=` is given (an interactive invocation), use `/tmp/predbat-triage/<issue-number>` and `mkdir -p` it yourself.

## 1. Read the issue and the comment

Fetch the issue with `gh issue view <number> --json title,body,author,labels,comments` and find the comment whose `url` matches the argument. Work out the original problem from the issue body, the reporter's own comments, and the bot's triage comments (these open with "automated first-pass triage" or "automated follow-up triage review"). Work out the commenter's problem from their comment.

If the comment turns out not to be a report of a problem, e.g. a workaround, a question to the reporter, or a thank-you, stop without posting anything.

## 2. Look at their attachments only as far as the symptom

If the comment links a log or debug yaml, you may download it into the scratch directory and grep it to confirm the **symptom**, e.g. the error text, inverter type, Predbat version, or the mode involved. Never read a whole file into context. Stop as soon as you can tell the symptom. Don't follow it to a cause.

## 3. Decide: related or clearly unrelated

Compare the observable symptom, not a guessed cause. The default is **related**. Only call it **unrelated** when the difference is clear:

- **Unrelated** if any of these clearly differ: the error or traceback, the entity or integration involved, the mode (charging vs exporting vs holding), or the direction of the problem (too much vs too little). A different inverter brand alone does **not** make it different when the original is not brand-specific.
- **Related** otherwise, including when the comment doesn't say enough to tell. Don't assert that it is the same problem, though: later reviews still weigh it, and may still find it differs once there is more to go on.

## 4. Post only if unrelated

If the post is **related**, stop without posting anything. Silence leaves it in the thread as evidence, and a comment saying "looks the same" would pre-empt the analysis.

If it is **unrelated**, check first that no comment already contains `Looks unrelated to this issue: <comment-url>` for this comment, whether from an earlier me-too check or a follow-up review. If one does, stop.

Post exactly one comment via `gh issue comment <number> --body "..."`:

- Open with a line disclosing this is an automated me-too check (a maintainer will review before any action is taken).
- Then write the record line exactly like this, with the comment's full URL: `Looks unrelated to this issue: <comment-url>`. Later reviews search for this line to leave the post out of their analysis.
- Address the commenter by `@login` and say in one plain sentence what differs.
- Ask them to open a new issue using the bug report template, with their own `predbat.log` and `predbat_debug.yaml`, and to link back here. Say that their post won't be used in the analysis of this issue, and that a maintainer can reverse that if the bot got it wrong.

Keep it short. The comment is for the commenter, not a discussion of the original problem.

## Guardrails

- Analysis only: no commits, no pushes, no PRs, no code changes that leave this clone.
- Never add or remove labels, never close or edit the issue, and never touch `waiting_for_user`. This check is about the commenter, not the reporter.
- Never give a theory, cause or workaround for either problem.
- If a command you needed was blocked by permissions, say plainly in the comment what you could not do.
