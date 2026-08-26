---
name: issue-triage-followup
description: Re-review an already-triaged batpred GitHub issue in light of information added since the original triage, and post a follow-up comment updating the assessment if warranted.
allowed-tools: You can view and edit files in this repo, run local tests, download and grep issue attachments, access the issue in github with 'gh' and use git commands to check history and re-sync the clone. Do not go outside this sandbox or push any changes back to git.
---

# Issue Triage Follow-up

You are re-reviewing one already-triaged GitHub issue on `springfall2008/batpred`, incorporating anything added since the bot's previous triage comment. The working directory is a dedicated local clone of the repo — investigate using it directly.

Arguments: `<issue-number> [scratch=<dir>]`, same convention as `/issue-triage`. If no `scratch=` is given (an interactive invocation), use `/tmp/predbat-triage/<issue-number>` and `mkdir -p` it yourself.

## 1. Read the issue and find what's new

Fetch it with `gh issue view <number> --json title,body,labels,comments`. Find the bot's most recent prior comment (opens with "automated first-pass triage" or "automated follow-up triage review"). Everything posted **after** that comment — reporter replies, new logs, a maintainer's note — is the new information this review exists to act on.

If there's nothing posted since the last bot comment, say so plainly in your follow-up comment rather than inventing a reason to change anything.

## 2. Fetch any new attachments

Same as `/issue-triage` step 2: if the new information links a log file, a `predbat_debug.yaml`, or a zip of either, download it into the scratch directory and size/grep it rather than reading it whole.

## 3. Re-investigate against current main

- Sync the clone first, so you are reading the code you think you are:

  ```bash
  git fetch origin main && git checkout main && git reset --hard origin/main && git clean -fd
  git describe --tags
  ```

- Read [../issue-triage/references/debug-journal.md](../issue-triage/references/debug-journal.md) before revising your assessment.
- Re-check `git log`/`git blame` on the relevant area — the fix may have landed on `main` since the original triage.
- If the new information points at a test module, run it via `tools/triage_test.sh <name> <scratch>/test.log` (never the full suite).

## 4. Decide whether the assessment changes

Compare your updated understanding against the original triage comment's classification, priority, and root-cause pointer:

- If nothing material changed, say so — confirming the original assessment still holds is a legitimate outcome, not a reason to avoid posting.
- If the classification, priority, or root-cause should change given the new information, update it. You may change or remove a label **you previously applied as the bot** (classification, priority, `waiting_for_user`) — never remove a label a human added.
- Apply the same duplicate check as `/issue-triage` step 4 if the new information suggests one.

## 5. Post one follow-up comment

Check the comment history first: if the most recent comment is already a bot comment with nothing from a human posted after it, stop — this review already happened and nothing new has arrived since.

Post exactly one comment via `gh issue comment <number> --body "..."`, opening with a line disclosing this is an automated follow-up triage review (a maintainer will review before any action is taken), followed by: what was new since the last review, whether the classification/priority/root-cause changed and why (or confirmation nothing changed), and any further information request.

Do **not** touch the `BOT_TRIAGED` or `BOT_REVIEW` labels — the daemon manages both.

## Guardrails

- Analysis only: no commits, no pushes, no PRs, no code changes that leave this clone.
- Never remove a label a human applied; you may only revise labels the bot itself applied.
- Never close an issue except a confident duplicate.
- If a command you needed was blocked by permissions, say plainly in the comment what you could not do — never word it so a step you skipped reads as one you completed.
