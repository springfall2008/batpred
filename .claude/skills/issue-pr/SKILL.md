---
name: issue-pr
description: Implement the fix or feature described in an already-triaged batpred GitHub issue and open a draft pull request referencing it, for maintainer review.
allowed-tools: You can view and edit files in this repo, write code and tests, run local tests and pre-commit, commit and push a new branch, and open a draft pull request with 'gh'. Do not merge, close, or push directly to main.
---

# Issue PR

You are implementing the fix or feature described in one already-triaged GitHub issue on `springfall2008/batpred`, and opening a draft pull request for maintainer review. The working directory is the same dedicated local clone the triage skill uses — investigate and work using it directly.

Arguments: `<issue-number> [scratch=<dir>]`, same convention as `/issue-triage`. If no `scratch=` is given (an interactive invocation), use `/tmp/predbat-triage/<issue-number>` and `mkdir -p` it yourself.

This skill assumes the issue has already been triaged — the daemon only invokes it once that's confirmed. Read the existing triage comment before doing anything else.

## 1. Read the ticket

Fetch it with `gh issue view <number> --json title,body,labels,comments`. The bot's own triage comment (opens with "automated first-pass triage") already has the classification, priority, and any root-cause pointer — start from there rather than re-investigating from scratch.

## 2. Investigate

- Sync the clone first, so you are reading the code you think you are:

  ```bash
  git fetch origin main && git reset --hard origin/main && git clean -fd
  git describe --tags
  ```

- Read [../issue-triage/references/debug-journal.md](../issue-triage/references/debug-journal.md) before forming an implementation approach — it maps common symptoms to modules and records known per-integration behaviour from past investigations. Its entries are dated observations, not current truth — confirm anything you rely on against the working tree.
- Read the relevant source area named in the triage comment's root-cause pointer, or that the issue's classification points to.
- Check `git log`/`git blame` on that area — the triage comment may already cover this, but confirm nothing has changed on `main` since triage ran.

## 3. Implement

- Write the fix or feature following this repo's conventions (`CLAUDE.md`, already loaded automatically for this session): `lower_case_with_underscores` naming, 256-character line length, a one-line docstring on every new function or class.
- Add or update a unit test for the change, in the matching `apps/predbat/tests/test_<feature>.py` module (registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py` if it's a new module) — `CLAUDE.md` requires unit tests for all new code, no exceptions for bot-authored ones.
- Keep the change scoped to what the issue describes. Don't refactor unrelated code, even if you notice something else worth fixing.

## 4. Quality gate

Both of these must pass before you continue to step 5. `run_pre_commit` must run with `coverage/` as the working directory (it sources `coverage/setup.csh` internally); `tools/triage_test.sh` must run from the repo root. Change directory explicitly for each rather than assuming where you're left afterwards:

```bash
cd coverage
./run_pre_commit
cd ..
tools/triage_test.sh <name> <scratch>/test.log
```

Use the test module named in the triage comment, or the one `TEST_REGISTRY` maps to the area you changed. If there's no clean single-module mapping, run `./run_all --quick` instead (from `coverage/`, same as above) rather than guessing at a module name.

If either fails, stop here — do not commit, push, or open a PR. Go to step 7 and report what failed.

## 5. Branch, commit, push

Branch name: `fix/<slug>-<issue-number>` for a `bug` classification, `feat/<slug>-<issue-number>` for `enhancement` — matching this repo's existing convention (e.g. `fix/solis-tou-bit-refused-4707`). `<slug>` is a short kebab-case description of the change.

```bash
git checkout -B fix/<slug>-<issue-number>
git add <changed files>
git commit -m "<one-line summary of the fix>"
git push -u origin fix/<slug>-<issue-number>
```

`-B` rather than `-b`: if a previous attempt reached branch creation and then failed on push or PR creation, the local branch is left behind, and a retry's plain `-b` would fail with "branch already exists." `-B` resets it to the current `HEAD` instead, so a retry always starts clean.

Never push to `main`, and never force-push.

## 6. Open the draft PR

```bash
gh pr create --draft --assignee springfall2008 \
  --title "<one-line summary>" \
  --body "$(cat <<'EOF'
This is an automated draft PR generated from issue #<issue-number> — a maintainer should review it before merging.

Fixes #<issue-number>

## Summary

<what changed and why, in a sentence or two>

## Testing

<what you ran in step 4 and its result>

## Notes

<a debug-journal.md reference if one informed the approach, otherwise omit this section>
EOF
)"
```

This is the last step on success — there is nothing further to report; the daemon detects the new PR itself.

## 7. On failure

If step 4's quality gate failed, or an earlier step couldn't proceed (e.g. the fix genuinely needs information only a maintainer has), post exactly one comment on the issue via `gh issue comment <number> --body "..."`, opening with a line disclosing this is an automated PR-creation attempt (a maintainer will review before any action is taken), followed by what you attempted and what failed — never word it so a skipped step reads as one you completed (same guardrail as the triage skill). Then stop. Do not commit, push, or open a PR — the daemon detects this outcome itself by finding no PR referencing the issue afterwards.

## Guardrails

- Never push to `main` or force-push.
- Never merge, close, or edit an existing PR.
- Never remove a label a human applied.
- The PR is always a draft — never open it as ready-for-review.
- If a command you needed was blocked by permissions, say plainly in the failure comment what you could not do.
