---
name: issue-triage
description: Investigate a newly opened batpred GitHub issue against current main, classify it, assign labels and priority, and post a first-pass triage comment.
allowed-tools: Bash(gh *), Bash(git log*), Bash(git diff*), Bash(git show*), Bash(git blame*), Bash(./run_all*), Edit, WebFetch, Read, Grep, Glob
---

# Issue Triage

You are triaging one GitHub issue on `springfall2008/batpred`. The issue number is given as an argument to this skill invocation. The working directory is a dedicated local clone already synced to the current `main` branch — investigate using it directly.

## 1. Read the issue

Fetch it with `gh issue view <number> --json title,body,labels,comments`. Note any labels already applied — never remove a label a human added.

## 2. Fetch attachments

If the body links a log file or `predbat_debug.yaml`, fetch and read them. Use the log for error/traceback context and the debug yaml for the reporter's actual configuration.

## 3. Classify the type

Apply exactly one of: `bug`, `question`, `configuration` (user error/misconfiguration), `enhancement` (feature request). If genuinely ambiguous, apply `unclear` instead of guessing.

## 4. Check for duplicates

Search existing issues (`gh issue list --search ...`, both open and closed) for the same symptom.

- High confidence match: label `duplicate`, comment linking the original issue, and close this one.
- Low confidence: mention "possibly related to #N" in your triage comment; don't close.

## 5. Investigate against current main

This clone is already synced to `origin/main` — don't re-sync it yourself.

- Read the relevant source area for the reported symptom (e.g. `apps/predbat/fetch.py` for rate issues, `apps/predbat/inverter.py` for a named inverter).
- Check `git log` / `git blame` on that area for recent related changes — the issue may already be fixed on main since the version the reporter is using.
- If the issue clearly maps to an existing test module (`apps/predbat/tests/test_<feature>.py`, listed in `TEST_REGISTRY` in `unit_test.py`), run just that test: `cd coverage && ./run_all --test <name>`. Skip this step if there's no clean mapping — never run the full suite.
- Form a root-cause hypothesis with a `file:line` pointer if the investigation supports one. It's fine to say the cause needs maintainer review if it doesn't.
- You may edit files locally to test a hypothesis (e.g. a temporary debug print, a tweaked test fixture) — this clone is hard-reset before the next run, so nothing here persists. Never `git commit` or `git push`.

## 6. Apply component labels

Only when the issue text clearly names a specific inverter or integration (e.g. `solis`, `Huawei`, `solaredge`, `Octopus`, `fox`), apply the matching existing label. Don't guess.

## 7. Check for missing information

For bug reports specifically, verify these four things (from the bug report template) are actually present: Predbat version, inverter/environment details, log file, `predbat_debug.yaml`. If something missing is actually needed for your investigation, apply `waiting_for_user` and ask only for what's missing — don't ask for things already provided.

## 8. Set priority

Apply exactly one of:

- `priority_high` — impacts many users, no workaround
- `priority_medium` — impacts many users, workaround exists
- `priority_low` — limited impact or an easy workaround

Skip priority for pure questions and feature requests.

## 9. Post one comment

Check existing comments first — if one from you is already there, stop; don't post again.

Post exactly one comment via `gh issue comment <number> --body "..."`, opening with a line disclosing this is an automated first-pass triage (a maintainer will review before any action is taken), followed by: classification, priority (if set), what you investigated and found (including test result if you ran one), a root-cause pointer if you have one, and any information request.

## Guardrails

- Analysis only: no commits, no pushes, no PRs, no code changes that leave this clone.
- Never remove a label a human applied.
- Never close an issue except a confident duplicate.
- Don't run the full test suite — targeted single tests only, and only when clearly mapped.
