---
name: pr-review
description: Review a batpred pull request with the built-in /code-review, check each finding's anchor against the PR's diff, and post the findings as one GitHub review.
allowed-tools: You can read files in this repo, view the PR and its diff with 'gh', write files in the scratch directory, and post one review on the PR through 'gh api'. Do not edit the repo, commit, push, approve, request changes, merge or close the PR.
---

# PR Review

You are reviewing one pull request on `springfall2008/batpred` and posting what the review finds as a single GitHub review. The working directory is the same dedicated local clone the triage skills use.

Arguments: `<pr-number> [scratch=<dir>] [dry-run]`, same convention as `/pr-cleanup`. If no `scratch=` is given (an interactive invocation), use `/tmp/predbat-triage/<pr-number>` and `mkdir -p` it yourself. `dry-run` means stop after step 4: write the review file, but do not post it.

## Why this skill exists

The built-in `/code-review` runs its review in a forked subagent. A fork does not see this skill's instructions or anything the caller appended to the system prompt, so when it was asked to post (`--comment`) it picked its own `gh api` spelling. On PR #5304 it passed the body as `-f body="$(cat file)"`, and the permission check denied every call. Here the fork only reviews, and this session - which can see these instructions - does the posting.

## 1. Run the review

Invoke the Skill tool with skill `code-review` and args `<pr-number> high`.

Never pass `--comment`. The fork would then try to post on its own, which is exactly what this skill replaces.

The Skill tool returns the review's final message verbatim. At `high` effort that message contains a JSON array of findings, each with `file`, `line`, `summary` and `failure_scenario`. If it came back as prose instead, extract the same four things from each `path:line` finding it names.

## 2. Get the PR's head and diff

```bash
gh pr view <pr-number> --json headRefOid --jq .headRefOid
gh pr diff <pr-number>
```

An inline review comment has to sit on a line that is inside a hunk on the new side of the diff; GitHub rejects the whole review otherwise. Work out each file's new-side line ranges from its `@@ -a,b +c,d @@` headers (lines `c` to `c+d-1`).

## 3. Check each finding's anchor

The reviewer's line numbers are not always right: on PR #5304 two of seven anchors were a line or two off the code they described, one sat just outside the diff, and three findings cited other lines that did not hold the code described - one past the end of the file. For each finding:

- Find the code the summary describes, in the diff hunk or with `Read` on the file. The clone is on `main`, so a line that only exists on the PR's branch has to be found in the diff, not in the file.
- If the cited line is wrong but the right line is inside a hunk, use the right line.
- If the code it describes is outside every hunk, or the file is not in the diff at all, it cannot be an inline comment. Put it in the review body instead, under a short `path:line` heading.
- If you cannot find the code it describes anywhere, keep the finding in the review body and say plainly that its location could not be confirmed.
- Correct any other line or file reference inside the finding's text the same way.

Do not re-review, re-rank, or drop findings - judging them is the reviewer's job. This step only makes sure each one lands where it belongs.

## 4. Write the review file

Write one JSON file with the Write tool at `<scratch>/review.json`:

```json
{
  "commit_id": "<head sha from step 2>",
  "event": "COMMENT",
  "body": "_Automated comment from the triage bot._\n\n<summary line>\n\n<findings that could not be anchored inline, if any>",
  "comments": [
    {"path": "apps/predbat/example.py", "line": 123, "side": "RIGHT", "body": "_Automated comment from the triage bot._\n\n<summary>\n\n<failure scenario>"}
  ]
}
```

- `event` is always `COMMENT`. Never `APPROVE` or `REQUEST_CHANGES`: a bot approving or blocking a merge is a governance action, not a comment.
- The review body and every inline comment body open with `_Automated comment from the triage bot._`, so a maintainer can tell bot feedback from a human reviewer's without checking the author.
- The review body's summary line names the head commit reviewed and how many findings are inline and how many are in the body.
- If the review found nothing, post a review with an empty `comments` list and a body saying the review at that head commit found no issues. Posting nothing reads as a failed run.

If `dry-run` was given, stop here and print the path of the review file.

## 5. Post it

Post the file exactly this way - one command, endpoint first, body read from the file:

```bash
gh api repos/springfall2008/batpred/pulls/<pr-number>/reviews --input <scratch>/review.json --jq .html_url
```

Do not post the findings one at a time through the `pulls/<pr-number>/comments` endpoint, and never put a comment body on the command line.

If GitHub answers 422 because a line could not be resolved, one of the anchors from step 3 is still outside the diff. Move every inline comment into the review body as a `path:line` list, set `comments` to `[]`, rewrite the file, and post once more.

If the call is denied, say so plainly in your final message and name the command. Do not fall back to printing the findings as though they had been posted.

## 6. Report

Finish with the review's URL, and the number of findings posted inline and in the body.
