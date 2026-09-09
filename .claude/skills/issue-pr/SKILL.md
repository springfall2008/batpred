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

- Read [tools/debug-journal.md](../../../tools/debug-journal.md) before forming an implementation approach — it maps common symptoms to modules and records known per-integration behaviour from past investigations. Its entries are dated observations, not current truth — confirm anything you rely on against the working tree.
- Read the relevant source area named in the triage comment's root-cause pointer, or that the issue's classification points to.
- Check `git log`/`git blame` on that area — the triage comment may already cover this, but confirm nothing has changed on `main` since triage ran.

## 3. Implement

- Write the fix or feature following this repo's conventions (`CLAUDE.md`, already loaded automatically for this session): `lower_case_with_underscores` naming, 256-character line length, a one-line docstring on every new function or class.
- Add or update a unit test for the change, in the matching `apps/predbat/tests/test_<feature>.py` module (registered in `TEST_REGISTRY` in `apps/predbat/unit_test.py` if it's a new module) — `CLAUDE.md` requires unit tests for all new code, no exceptions for bot-authored ones.
- Keep the change scoped to what the issue describes. Don't refactor unrelated code, even if you notice something else worth fixing.

## 4. Quality gate

Both of these must pass before you continue to step 6. `run_pre_commit` must run with `coverage/` as the working directory (it sources `coverage/setup.csh` internally); `tools/triage_test.sh` must run from the repo root. Change directory explicitly for each rather than assuming where you're left afterwards:

```bash
cd coverage
./run_pre_commit
cd ..
git stash push -u -- <the source files you changed, NOT the test file>
tools/triage_test.sh <name> <scratch>/test-red.log     # expect FAILURE
git stash pop
tools/triage_test.sh <name> <scratch>/test.log         # expect PASS
```

Use the test module named in the triage comment, or the one `TEST_REGISTRY` maps to the area you changed. If there's no clean single-module mapping, run `./run_all --quick` instead (from `coverage/`, same as above) rather than guessing at a module name.

If `run_pre_commit` fails, or the second run does not pass, stop here — do not commit, push, or open a PR. Go to step 8 and report what failed.

### Why the module runs twice

A test that passes with and without your change is not coverage, it is decoration — and it is easy to write one by accident. The first run is the proof that the test can fail; the second is the gate. **A red run that passes is a failure of the check**, not a pass: stop and work out why before going further.

`-u` is not optional. Without it, `git stash push` **fails outright** on a fix that adds a new source file — it refuses with "Did you forget to 'git add'?" and stashes *nothing*, so the red run contains your entire fix, passes, and tells you the opposite of the truth. Check the stash actually happened before trusting a red run: the source file should be back to its committed content, and any new file gone.

Stash only the source change. If you stash the test too, both runs pass and you have proved nothing.

Report both outcomes in the PR body's Testing section — "fails without the fix, passes with it" is the sentence a reviewer is looking for.

If `git stash pop` reports a conflict, **stop and go to step 8**. Do not improvise another way to restore the change; say the fix is stashed and needs recovering by hand.

Some fixes genuinely cannot be made to fail on this harness, and that is a real finding rather than an excuse. The GH#4965 guard is the worked example: deleting the attribute it protects does not reproduce the crash, because the mock inverter never reaches the read. When that happens, say which and why in the PR body, and make the test assert the guard is present rather than dressing up a reproduction that would pass either way.

## 5. Review your own diff

The gate above proves the change works. It does not prove the change is any good, and nothing else reads your diff before a maintainer does. So read it:

```bash
git diff
```

Four things the tests cannot catch, in the order they go wrong most often on this bot's own PRs:

- **Names that stopped being true.** A variable or function whose meaning you widened but whose name you left behind. On PR #4957 a `location_at_home` flag ended up true for dispatches that were explicitly *not* at home — no test could fail on that, and the next reader would have "fixed" the inconsistency by reintroducing the bug.
- **Logic you duplicated.** If what you added already exists a few lines up in a different shape, share it or say in the PR body why you deliberately did not. The same review found the same predicate in three unshared copies with three different meanings.
- **Blast radius.** `CLAUDE.md` requires an `impact()` call before changing any symbol — run it for anything whose signature or behaviour you changed. If the GitNexus tools are not available in your session, say so in the PR body and establish the callers by search instead. Do not skip it silently.
- **Scope and leftovers.** Does the diff do what the ticket asked and only that? Debug prints, a stray file, a refactor you talked yourself into — this is the only point they get caught.

Fix what you find and re-run step 4. If you decide something is not worth fixing, name it in the PR body's Notes section: a reviewer who sees it named reads it as a judgement, and a reviewer who finds it themselves reads it as an oversight.

## 6. Branch, commit, push

Branch name: `fix/<slug>-<issue-number>` for a `bug` classification, `feat/<slug>-<issue-number>` for `enhancement` — matching this repo's existing convention (e.g. `fix/solis-tou-bit-refused-4707`). `<slug>` is a short kebab-case description of the change.

```bash
git checkout -B fix/<slug>-<issue-number>
git add <changed files>
git commit -m "<one-line summary of the fix>"
git push -u origin fix/<slug>-<issue-number>
```

`-B` rather than `-b`: if a previous attempt reached branch creation and then failed on push or PR creation, the local branch is left behind, and a retry's plain `-b` would fail with "branch already exists." `-B` resets it to the current `HEAD` instead, so a retry always starts clean.

Never push to `main`, and never force-push.

## 7. Open the draft PR

```bash
gh pr create --draft --assignee springfall2008 \
  --title "<one-line summary>" \
  --body "$(cat <<'EOF'
This is an automated draft PR generated from issue #<issue-number> — a maintainer should review it before merging.

Fixes #<issue-number>

## Summary

<what changed and why, in a sentence or two>

## Testing

<what you ran in step 4 and its result, including whether the new test fails without the fix>

## Notes

<anything step 5 turned up that you decided not to fix, and why; a debug-journal.md reference if one informed the approach; and a line if the GitNexus tools were unavailable so the blast-radius check was done by search. Omit this section if none apply>
EOF
)"
```

This is the last step on success — there is nothing further to report; the daemon detects the new PR itself.

## 8. On failure

If step 4's quality gate failed, or an earlier step couldn't proceed (e.g. the fix genuinely needs information only a maintainer has), post exactly one comment on the issue via `gh issue comment <number> --body "..."`, opening with a line disclosing this is an automated PR-creation attempt (a maintainer will review before any action is taken), followed by what you attempted and what failed — never word it so a skipped step reads as one you completed (same guardrail as the triage skill). Then stop. Do not commit, push, or open a PR — the daemon detects this outcome itself by finding no PR referencing the issue afterwards.

## Guardrails

- Never push to `main` or force-push.
- Never merge, close, or edit an existing PR.
- Never remove a label a human applied.
- The PR is always a draft — never open it as ready-for-review.
- If a command you needed was blocked by permissions, say plainly in the failure comment what you could not do.
