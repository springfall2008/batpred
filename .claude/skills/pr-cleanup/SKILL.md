---
name: pr-cleanup
description: Address code review feedback and CI failures on a batpred pull request, then commit and push the fixes back to its branch.
allowed-tools: You can view and edit files in this repo, write code and tests, run local tests and pre-commit, commit and push to the PR's own branch, and reply to review comments with 'gh'. Do not merge, close, or push to main.
---

# PR Cleanup

You are addressing outstanding feedback and CI failures on one pull request on `springfall2008/batpred`, then committing and pushing the fixes back to its branch. The working directory is the same dedicated local clone the triage skill uses.

Before implementing anything, load the `superpowers:receiving-code-review` skill and follow its evaluation discipline for every piece of feedback you act on: verify against the codebase before implementing, push back with technical reasoning on anything that seems wrong or unclear, and never perform agreement ("You're absolutely right!", "Great point!"). That skill assumes a live human partner to ask when something is unclear — you don't have one here. Wherever it says to stop and ask your human partner, instead post one comment on the PR explaining what's unclear and stop (see step 6).

Arguments: `<pr-number> [scratch=<dir>]`, same convention as `/issue-triage`. If no `scratch=` is given (an interactive invocation), use `/tmp/predbat-triage/<pr-number>` and `mkdir -p` it yourself.

## 1. Check out the PR's branch

```bash
gh pr checkout <pr-number>
git fetch origin main
```

Work on the PR's own branch, not a new one — you're pushing back to it directly, not opening a separate PR.

## 2. Gather everything to address

- All feedback on the PR, not just a prior bot review if there was one: `gh pr view <pr-number> --json comments` for top-level comments, and `gh api repos/springfall2008/batpred/pulls/<pr-number>/comments` for inline review-thread comments — `gh pr view` does not surface these.
- CI status: `gh pr checks <pr-number>`. For any failing check, read its actual log (`gh run view <run-id> --job <job-id> --log`, or fetch the job log directly) to find the real failure, not just the pass/fail summary.
- Skip anything already resolved: a review thread that already has a reply from you addressing it, or a check that's since gone green.

If there is genuinely nothing new to address — every thread already has a reply, every check is green — say so in a brief comment and stop; this is a normal, successful outcome, not a failure.

## 3. Evaluate before implementing

Per `receiving-code-review`: for each remaining item, verify it's technically correct for this codebase before touching anything. Some feedback will be wrong, outdated, or already addressed elsewhere in the diff — say so in a thread reply rather than implementing it anyway.

## 4. Implement confirmed fixes

Same conventions as `/issue-pr`: `CLAUDE.md` (already loaded automatically for this session), `lower_case_with_underscores` naming, 256-character line length, a one-line docstring on every new function or class, and a unit test for any new code.

## 5. Quality gate

Both of these must pass before you commit anything. `run_pre_commit` must run with `coverage/` as the working directory (it sources `coverage/setup.csh` internally); `tools/triage_test.sh` must run from the repo root:

```bash
cd coverage
./run_pre_commit
cd ..
tools/triage_test.sh <name> <scratch>/test.log
```

Use the test module the changed area maps to in `TEST_REGISTRY` (`apps/predbat/unit_test.py`), or `./run_all --quick` (from `coverage/`, same as above) if there's no clean single-module mapping. If either still fails after your fixes, go to step 6 and report what's still broken — do not commit or push a change that doesn't pass its own quality gate.

## 6. Commit, push, and reply

```bash
git add <changed files>
git commit -m "<one-line summary of the fixes>"
git push
```

Reply to each review thread you addressed, in the thread itself, not as a new top-level comment:

```bash
gh api repos/springfall2008/batpred/pulls/<pr-number>/comments/<comment-id>/replies -f body="..."
```

State what changed, or push back with your reasoning if you didn't implement the suggestion — never a bare "done" with no explanation. If anything is unclear, technically wrong, or you can't verify a suggestion, say so in the reply rather than guessing or implementing something you don't understand.

If step 5's quality gate never passed, post one summary comment via `gh pr comment <pr-number> --body "..."` explaining what's still failing and why you didn't push, instead of doing the commit/push/reply steps above.

## Guardrails

- Never push to `main` or force-push. Only push to the PR's own branch.
- Never merge or close the PR.
- Never remove a label a human applied.
- Never perform agreement ("You're absolutely right!", "Great point!", "Thanks for catching that!") — state the fix or the pushback plainly, per `receiving-code-review`.
- If a command you needed was blocked by permissions, say so plainly rather than implying a step completed.
