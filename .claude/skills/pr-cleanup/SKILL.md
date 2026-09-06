---
name: pr-cleanup
description: Address code review feedback and CI failures on a batpred pull request, then commit and push the fixes back to its branch.
allowed-tools: You can view and edit files in this repo, write code and tests, run local tests and pre-commit, merge origin/main into the PR's branch to resolve conflicts, commit and push to the PR's own branch, and reply to review comments with 'gh'. Do not merge or close the PR itself, and do not push to main.
---

# PR Cleanup

You are addressing outstanding feedback and CI failures on one pull request on `springfall2008/batpred`, then committing and pushing the fixes back to its branch. The working directory is the same dedicated local clone the triage skill uses.

Before implementing anything, load the `superpowers:receiving-code-review` skill and follow its evaluation discipline for every piece of feedback you act on: verify against the codebase before implementing, push back with technical reasoning on anything that seems wrong or unclear, and never perform agreement ("You're absolutely right!", "Great point!"). That skill assumes a live human partner to ask when something is unclear — you don't have one here. Wherever it says to stop and ask your human partner, instead post one comment on the PR explaining what's unclear and stop (see step 7).

Arguments: `<pr-number> [scratch=<dir>]`, same convention as `/issue-triage`. If no `scratch=` is given (an interactive invocation), use `/tmp/predbat-triage/<pr-number>` and `mkdir -p` it yourself.

## 1. Check out the PR's branch

```bash
gh pr checkout <pr-number>
git fetch origin main
```

Work on the PR's own branch, not a new one — you're pushing back to it directly, not opening a separate PR.

## 2. Sync with main and resolve conflicts

```bash
git merge --no-edit origin/main
```

`--no-edit` avoids hanging on an interactive editor prompt for the merge commit message - there's no one here to dismiss it.

If it says "Already up to date", there's nothing to do here — move on to step 3.

Otherwise the merge either completes cleanly (a fast-forward, which creates no new commit, or an automatic merge, which does — either way there is now something to push even if step 3 turns up no other feedback) or stops with conflicts. For conflicts, resolve each conflicted file by hand:

- Read both sides of every `<<<<<<<`/`=======`/`>>>>>>>` block and understand *why* each side changed — `git log`/`git diff` on the conflicting commits on both this branch and `main` — before writing the resolution. Never resolve by blindly keeping "ours" or "theirs" wholesale; combine the intent of both sides.
- After editing, confirm no conflict markers remain anywhere: `git grep -n -e '^<<<<<<<' -e '^=======' -e '^>>>>>>>'` should find nothing.
- `git add` the resolved files and `git commit --no-edit` (the default merge commit message is fine) to complete the merge.

If the conflicts are extensive enough that a safe resolution would mean re-implementing significant logic, or you are not confident the resolution preserves both sides' intent, do not guess: run `git merge --abort` to return to a clean state, then post one comment on the PR explaining the branch needs a manual rebase and naming what changed upstream that makes it non-trivial. Then stop, the same way as an unpassable quality gate (step 6) — do not attempt any further steps.

## 3. Gather everything to address

- All feedback on the PR, not just a prior bot review if there was one: `gh pr view <pr-number> --json comments` for top-level comments, and `gh api repos/springfall2008/batpred/pulls/<pr-number>/comments` for inline review-thread comments — `gh pr view` does not surface these.
- CI status: `gh pr checks <pr-number>`. For any failing check, read its actual log (`gh run view <run-id> --job <job-id> --log`, or fetch the job log directly) to find the real failure, not just the pass/fail summary.
- Skip anything already resolved: a review thread that already has a reply from you addressing it, or a check that's since gone green.

If there is genuinely nothing new to address — step 2's merge was a no-op, every thread already has a reply, every check is green — post a brief comment saying so, opening with a line disclosing this is an automated cleanup check (nothing for a maintainer to act on),
and stop; this is a normal, successful outcome, not a failure.

## 4. Evaluate before implementing

Per `receiving-code-review`: for each remaining item, verify it's technically correct for this codebase before touching anything. Some feedback will be wrong, outdated, or already addressed elsewhere in the diff — say so in a thread reply rather than implementing it anyway.

## 5. Implement confirmed fixes

Same conventions as `/issue-pr`: `CLAUDE.md` (already loaded automatically for this session), `lower_case_with_underscores` naming, 256-character line length, a one-line docstring on every new function or class,
and a unit test for any new code.

## 6. Quality gate

Both of these must pass before you push anything - this covers step 2's merge as much as any fix from step 5, so run it even if step 5 had nothing to do. `run_pre_commit` must run with `coverage/` as the working directory
(it sources `coverage/setup.csh` internally); `tools/triage_test.sh` must run from the repo root:

```bash
cd coverage
./run_pre_commit
cd ..
tools/triage_test.sh <name> <scratch>/test.log
```

Use the test module the changed area maps to in `TEST_REGISTRY` (`apps/predbat/unit_test.py`), or `./run_all --quick` (from `coverage/`, same as above) if there's no clean single-module mapping.
If either still fails, go to step 7 and report what's still broken — do not push a change that doesn't pass its own quality gate,
even if step 2's merge commit already exists locally: an unpushed local commit is discarded automatically the next time this runs.

## 7. Commit, push, and reply

```bash
git add <changed files>
git commit -m "<one-line summary of the fixes>"
git push
```

Skip the commit if step 5 had nothing to implement — step 2's merge commit (if any) is already complete and just needs pushing.
Reply to each review thread you addressed, in the thread itself, not as a new top-level comment. Open every reply with a short line disclosing it is an automated reply from the triage bot, then state what changed:

```bash
gh api repos/springfall2008/batpred/pulls/<pr-number>/comments/<comment-id>/replies -f body="..."
```

Or push back with your reasoning if you didn't implement the suggestion — never a bare "done" with no explanation. If anything is unclear, technically wrong, or you can't verify a suggestion, say so in the reply rather than guessing or implementing something you don't understand.

If step 6's quality gate never passed, post one summary comment via `gh pr comment <pr-number> --body "..."` opening with the same automated-reply disclosure line, explaining what's still failing and why you didn't push,
instead of doing the commit/push/reply steps above.

## Guardrails

- Never push to `main` or force-push. Only push to the PR's own branch.
- Never merge or close the PR itself (via `gh pr merge`/`gh pr close`) — merging `origin/main` into the branch to resolve conflicts, per step 2, is expected and not what this guardrail means.
- Only ever merge `origin/main` into the branch, never any other ref or direction.
- Never remove a label a human applied.
- Never perform agreement ("You're absolutely right!", "Great point!", "Thanks for catching that!") — state the fix or the pushback plainly, per `receiving-code-review`.
- If a command you needed was blocked by permissions, say so plainly rather than implying a step completed.
