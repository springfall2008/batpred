---
name: issue-triage
description: Investigate a newly opened batpred GitHub issue against current main, classify it, assign labels and priority, and post a first-pass triage comment.
allowed-tools: You can view and edit files in this repo, run local tests, download and grep issue attachments, access the issue in github with 'gh' and use git commands to check history and re-sync the clone. Do not go outside this sandbox or push any changes back to git.
---

# Issue Triage

You are triaging one GitHub issue on `springfall2008/batpred`. The working directory is a dedicated local clone of the repo — investigate using it directly.

Arguments: `<issue-number> [scratch=<dir>]`. The scratch directory is a writable place to download attachments to; it is emptied before each run. If no `scratch=` is given (an interactive invocation), use `/tmp/predbat-triage/<issue-number>` and `mkdir -p` it yourself.

## 1. Read the issue

Fetch it with `gh issue view <number> --json title,body,labels,comments`. Note any labels already applied — never remove a label a human added.

## 2. Fetch attachments

If the body links a log file, a `predbat_debug.yaml`, or a zip of either, download it into the scratch directory rather than pulling it through WebFetch — a `predbat.log` is routinely tens of MB, well past what WebFetch will return.

```bash
curl -sL "<url>" -o <scratch>/predbat.log
```

Never `cat` or read a whole log into context. Size it up first, then extract only what matters:

```bash
wc -l <scratch>/predbat.log
grep -n "Traceback\|ERROR\|Exception\|Warn" <scratch>/predbat.log | tail -50
sed -n '<start>,<end>p' <scratch>/predbat.log     # context around an interesting line
```

The log gives you error/traceback context; the debug yaml gives you the reporter's actual configuration (grep it for the specific keys you care about if it is large). The reporter's Predbat version is usually in the first few lines of the log — quote the version you actually confirmed, not the one in the issue template.

A `predbat_debug.yaml` can also be replayed against current main to reproduce their plan and list every setting they have changed from default — see [references/debug-journal.md](references/debug-journal.md).

## 3. Classify the type

Apply exactly one of: `bug`, `question`, `configuration` (user error/misconfiguration), `enhancement` (feature request). If genuinely ambiguous, apply `unclear` instead of guessing.

## 4. Check for duplicates

Search existing issues (`gh issue list --search ...`, both open and closed) for the same symptom.

- High confidence match: label `duplicate`, comment linking the original issue, and close this one.
- Low confidence: mention "possibly related to #N" in your triage comment; don't close.

## 5. Investigate against current main

- Sync the clone first and discard anything left over from a previous run, so you are reading the code you think you are:

  ```bash
  git fetch origin main && git reset --hard origin/main && git clean -fd
  git describe --tags        # the version you are investigating against
  ```

- Read [references/debug-journal.md](references/debug-journal.md) before forming a hypothesis. It maps common symptoms to modules, records known per-integration behaviour from past investigations, and lists the traps that have wasted time before. Its entries are dated observations, not current truth — confirm anything you rely on against the working tree.
- Read the relevant source area for the reported symptom (e.g. `apps/predbat/fetch.py` for rate issues, `apps/predbat/inverter.py` for a named inverter).
- Check `git log` / `git blame` on that area for recent related changes — the issue may already be fixed on main since the version the reporter is using.
- If the issue clearly maps to an existing test module (`apps/predbat/tests/test_<feature>.py`, listed in `TEST_REGISTRY` in `unit_test.py`), run just that test with the wrapper, from the repo root:

  ```bash
  tools/triage_test.sh <name> <scratch>/test.log
  ```

  It prints the exit status and the last 30 lines; grep the log file for anything more. Use the wrapper rather than composing your own `cd coverage && ./run_all ... > log` — a `cd` combined with an output redirect is refused outright in this session's permission mode, which is why the wrapper exists.

  Skip this step if there's no clean mapping — never run the full suite. A test only settles questions about code behaviour; it can't settle what real hardware did, so don't run one to look thorough.

- Form a root-cause hypothesis with a `file:line` pointer if the investigation supports one. It's fine to say the cause needs maintainer review if it doesn't.
- You may edit files locally to test a hypothesis (e.g. a temporary debug print, a tweaked test fixture) — this clone is hard-reset and cleaned before the next run, so nothing here persists. Never `git commit` or `git push`.

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

After posting, apply the `BOT_TRIAGED` label via `gh issue edit <number> --add-label BOT_TRIAGED` — every triage run gets this label, including a duplicate-close, regardless of classification. It marks the issue as triaged for the separate PR-creation flow (see `.claude/skills/issue-pr/SKILL.md`).

## Guardrails

- Analysis only: no commits, no pushes, no PRs, no code changes that leave this clone.
- Never remove a label a human applied.
- Never close an issue except a confident duplicate.
- If a command you needed was blocked by permissions, say plainly in the comment what you could not do — never word it so a step you skipped reads as one you completed.
