---
name: pr-review
description: Review a batpred pull request for correctness bugs in this session, check each finding against the PR's own code and diff, and post the findings as one GitHub review.
allowed-tools: You can read files in this repo, fetch and check out the PR's head commit, view the PR and its diff with 'gh', write files in the scratch directory, and post one review on the PR through 'gh api'. Do not edit the repo, run the PR's code, commit, push, approve, request changes, merge or close the PR.
---

# PR Review

You are reviewing one pull request on `springfall2008/batpred` and posting what you find as a single GitHub review. The working directory is the same dedicated local clone the triage skills use.

Arguments: `<pr-number> [scratch=<dir>] [dry-run]`, same convention as `/pr-cleanup`. If no `scratch=` is given (an interactive invocation), use `/tmp/predbat-triage/<pr-number>` and `mkdir -p` it yourself. `dry-run` means stop after step 7: write the review file, but do not post it.

Do the whole review yourself, in this session. Do not invoke `/code-review` or any other review skill, and do not start subagents. A forked review sees neither these instructions nor the daemon's appended system prompt, so the posting rules, the disclosure line and the journal request never reached it, and its reviews kept failing to post (PR #5304, #5574).

## 1. Check out the PR's head

```bash
gh pr view <pr-number> --json number,title,body,headRefOid,baseRefName,files,isCrossRepository
git fetch origin main
git fetch origin pull/<pr-number>/head
git checkout --detach FETCH_HEAD
git rev-parse HEAD
gh pr diff <pr-number> > <scratch>/pr.diff
```

Run each command from the clone as written. A form like `git -C <path> fetch` does not match the permission rules and is denied, which on PR #5574 left the review reading `main` instead of the PR.

- `git rev-parse HEAD` must equal `headRefOid`. If it does not, the PR moved between the two calls: fetch and check out again.
- If the PR body describes changes that `<scratch>/pr.diff` does not contain, check whether they have already reached `main`, for example through another PR for the same issue. If so, the PR may now be superseded, and the review should say so.
- Read code from the working tree from now on. It is the PR's version, so `Read` and `Grep` show what the PR actually ships. To see what `main` had, use `git show origin/main:<path>`.
- Read `<scratch>/pr.diff` for the diff. It is the diff GitHub anchors review comments on, so later steps check anchors against it.
- The GitNexus index describes `main`, not this checkout. Use it for callers and blast radius of symbols that already exist on `main`, and use `Grep` for anything the PR adds.
- Never run the PR's code: no tests, no `run_all`, no `python3` on its files. A PR from a fork can contain anything, and this session holds a credential that can post to the repo. The review is a reading exercise.

`sync_repo()` puts the clone back on `main` before the next flow, so leaving it detached here is fine. Do not edit any file in the clone.

## 2. Read the context

- The PR body and any issue it links (`gh issue view <n> --json title,body,comments`): what the change claims to fix, and what the reporter actually saw.
- `CLAUDE.md` at the repo root, which is already loaded. Its rules are the conventions angle in step 3.
- The sections of `tools/debug-journal.md` that cover the modules the diff touches, including "Symptom → first place to look" and "Traps when investigating". Journal entries are observations from past investigations, not statements about current code. Confirm against the checkout before relying on one.

## 3. Find candidate findings

Work through each angle below in turn, on the whole diff. Read the enclosing function of every hunk, not just the changed lines: a bug in an unchanged line of a touched function is in scope, because the PR re-exposes it or fails to fix it. Record every candidate as `path:line`, a one-sentence summary, and a concrete failure scenario: what input, state or timing leads to what wrong result. Do not let one angle's conclusion stop you recording another's.

- **Line by line.** For each changed line, ask what input, state or timing makes it wrong. Look for inverted conditions, off-by-one errors, `None` dereferences, falsy-zero checks (`if value:` where `0` is valid), wrong-variable copy-paste, and swallowed exceptions.
- **Removed behaviour.** For each deleted or replaced line, name the guard, error path or invariant it enforced, and find where the new code re-establishes it. If you cannot find it, that is a candidate.
- **Callers and callees.** For each changed function, find its callers (GitNexus `impact`/`context`, or `Grep`) and check whether the change breaks one: a new precondition, a changed return shape, a new exception, a new ordering dependency.
- **Python pitfalls.** Mutable default arguments, late-binding closures, dict mutation during iteration, float equality, naive and aware `datetime` mixed together, timezone and DST handling, integer division.
- **Time of day.** Predbat works in minutes since midnight (`minutes_now`) over a 48-hour horizon. Check windows that cross midnight, the last slot of the day, and DST changeover days. A test that sets `minutes_now = 0` cannot catch an off-by-one in `tod <= minutes_now`.
- **Maintainer rules.** These are the project maintainer's rules for every change. Apply them to the lines the PR adds or changes, not to code it leaves alone: much existing code predates them, and a review that lists every old instance buries the new one.
    - **Tests cover the change.** Every code change comes with tests that exercise it: new code, and changed behaviour in existing code. A test that would still pass with the change reverted does not count. For a bug fix, say whether the PR's test fails without the fix. New test modules must be registered in `TEST_REGISTRY` (`apps/predbat/unit_test.py`).
    - **User-facing changes are documented.** A new or changed setting, `apps.yaml` option, entity, sensor, service, web UI behaviour or default needs a matching update under `docs/`, and a new docs page needs adding to `mkdocs.yml`. Name the docs page that should change. A new setting also needs its `CONFIG_ITEMS` or `APPS_SCHEMA` entry (`apps/predbat/config.py`).
    - **No `getattr()` without a very good reason.** `getattr(obj, "name", default)` hides a typo or a missing initialisation behind a silent default. Prefer the attribute itself, initialised in `__init__`. Accept it only where the PR shows why the attribute can legitimately be absent, such as an optional duck-typed collaborator, and say what reason you accepted.
    - **Components go through `ComponentBase`.** Code in a component (a subclass of `ComponentBase`, `apps/predbat/component_base.py`) does not reach into `self.base.` directly. It uses the `ComponentBase` helpers (`get_arg`, `set_arg`, `get_state_wrapper`, `set_state_wrapper`, `now_utc`, `minutes_now`, `storage`, `call_notify` and the rest). If the helper it needs does not exist, the fix is to add one to `ComponentBase`, and the finding should say so.
    - **No extra complexity.** Flag complexity the change does not need: state that can be derived, a new abstraction with one caller, copy-paste with small variations, deep nesting, configuration nobody asked for, dead code left behind. Name the simpler form that does the same job.
    - **SOLID design.** Flag a concrete break, and name its cost:
        - **Single responsibility:** one function or class now does two unrelated jobs.
        - **Open/closed:** a new inverter, tariff or integration added by editing a shared `if`/`elif` chain, rather than through the existing per-type definition or component.
        - **Liskov substitution:** a subclass or component that breaks its base class's contract, e.g. an `api_start`/`api_stop` that does not behave as the lifecycle expects.
        - **Interface segregation:** callers forced to depend on things they do not use.
        - **Dependency inversion:** high-level code reaching into a concrete collaborator's internals instead of the interface it offers.

      "Not SOLID" with no concrete break is not a finding.
- **Batpred conventions** (from `CLAUDE.md` and past regressions). Flag only a clear break, and quote the rule:
    - A change to the simulation in `Prediction.run_prediction()` (`apps/predbat/prediction.py`) with no matching change to `apps/predbat/prediction_kernel.cpp`. The C++ kernel is meant to be a bit-for-bit mirror, and `test_kernel_parity.py` checks it.
    - A test that assigns to `my_predbat.*` or `my_predbat.args[...]` without restoring it in a `finally`. Every test shares one fixture, so leaked state breaks an unrelated later test.
    - Direct file access where the Storage component should be used.
    - New long-lived state on `PredBat` that the debug dump (`create_debug_yaml()`) would serialise and that holds secrets or tuple-keyed dicts. Those keys must go in `DEBUG_EXCLUDE_LIST` (`apps/predbat/utils.py`), or the dump either leaks or cannot be replayed (#5569).
    - A function or class with no docstring, or a non-`lower_case_with_underscores` name.
- **Cloud integrations.** A cache filled from reads and also used to skip "redundant" writes goes wrong when a read lands shortly after a write and is still stale: the Fox scheduler lags 3–18 seconds. A field set or enum taken from vendor sample code rather than the published API spec. Retries or backoff that never reset.
- **Use of storage.** Never write files directly, use the Storage class. Use it to save state so that when PredBat restarts it doesn't have to perform a lot of expensive operations again (e.g. re-download a tariff or scan registers on an inverter).
- **Altitude.** Does the change fix the root cause, or patch one symptom of a mechanism that will produce the next one? If a deeper general fix exists, name it.
- **Gaps.** Finally, re-read the diff once more looking only for defects not already listed: code moved or extracted that dropped a guard, setup/teardown asymmetry in tests, flipped config defaults, a golden case (`coverage/cases/*.expected.json`) that the change should have moved but did not.

## 4. Verify each candidate

For each candidate, go back to the checked-out code and confirm the failure scenario can actually happen:

- Trace it along real call paths: who calls this, with what values, at what point in the 5-minute update cycle? A bug on a path that production never takes is at most a low-severity finding. Say which path you checked.
- If the scenario needs a specific config, inverter or tariff, name it.
- If the PR's own tests cover the case and would fail on the bug, the candidate is wrong. Drop it.
- Drop exact duplicates (same defect, same place, same reason). Keep two findings on one line if their reasons differ.

Keep a candidate you could not fully confirm, but say in its text what you could not confirm. Do not drop a real but uncertain bug to make the review look cleaner.

A maintainer-rule finding has no crash to describe. Its failure scenario is the concrete cost: what the missing test would have caught, which docs page a user would read and find wrong, what the `getattr()` default hides, which future change the design makes harder.

Rank what survives, most severe first: correctness bugs, then maintainer rules (missing tests and missing docs first), then other convention breaks, then altitude. Keep at most 15. There is no minimum. If the PR is sound, a review saying so is the right result. Do not pad it.

## 5. Find each finding's anchor

An inline review comment has to sit on a line inside a hunk on the new side of the diff; GitHub rejects the whole review otherwise. Work out each file's new-side line ranges from the `@@ -a,b +c,d @@` headers in `<scratch>/pr.diff` (lines `c` to `c+d-1`).

- Because the working tree is the PR's head, a line number you read with `Read` is a new-side line number.
- If the code a finding describes is inside a hunk, anchor it on that line.
- If it is outside every hunk, or in a file the PR does not touch, it cannot be an inline comment. Put it in the review body instead, under a short `path:line` heading.
- Check every other `path:line` reference inside a finding's text the same way.

## 6. Write the review file

Write one JSON file with the Write tool at `<scratch>/review.json`:

```json
{
  "commit_id": "<headRefOid from step 1>",
  "event": "COMMENT",
  "body": "_Automated comment from the triage bot._\n\n<summary line>\n\n<findings that could not be anchored inline, if any>",
  "comments": [
    {"path": "apps/predbat/example.py", "line": 123, "side": "RIGHT", "body": "_Automated comment from the triage bot._\n\n<summary>\n\n<failure scenario, and what you verified>"}
  ]
}
```

- `event` is always `COMMENT`. Never `APPROVE` or `REQUEST_CHANGES`: a bot approving or blocking a merge is a governance action, not a comment.
- The review body and every inline comment body open with `_Automated comment from the triage bot._`, so a maintainer can tell bot feedback from a human reviewer's without checking the author.
- The review body's summary line names the head commit reviewed, and how many findings are inline and how many are in the body.
- If the review found nothing, still write a review: empty `comments`, and a body saying the review at that head commit found no issues, with one line on what you checked. Posting nothing reads as a failed run.

## 7. Journal finding

If the review turned up something a future triage run would want to know, write it to the journal queue as the appended system prompt describes, for example a trap in a module or a convention the codebase relies on that is written down nowhere. Usually there is nothing to write.

If `dry-run` was given, stop here and print the path of the review file.

## 8. Post it

Post the file exactly this way - one command, endpoint first, body read from the file:

```bash
gh api repos/springfall2008/batpred/pulls/<pr-number>/reviews --input <scratch>/review.json --jq .html_url
```

Do not post findings one at a time through the `pulls/<pr-number>/comments` endpoint, and never put a comment body on the command line.

If GitHub answers 422 because a line could not be resolved, one of the anchors from step 5 is still outside the diff. Move every inline comment into the review body as a `path:line` list, set `comments` to `[]`, rewrite the file, and post once more.

If the call is denied, say so plainly in your final message and name the exact command. Do not fall back to printing the findings as though they had been posted, and do not claim posting is impossible after one failed spelling.

## 9. Report

Finish with the review's URL, and the number of findings posted inline and in the body.
