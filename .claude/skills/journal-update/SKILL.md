---
name: journal-update
description: Fold the triage bot's queued findings into the debug journal, re-check the journal against what has merged since, and open a pull request for a maintainer to merge.
allowed-tools: You can read anything in this repo and the queue directory, but you may only edit two files - the debug journal and the cspell dictionary. You can run pre-commit, commit to a new bot/debug-journal-* branch, push that branch, and open a pull request. You cannot merge or close it, and you cannot push to main.
---

# Journal Update

You maintain `.claude/skills/issue-triage/references/debug-journal.md` — the file every triage, PR-review and PR-cleanup run reads before forming a hypothesis. Each of those runs can leave a finding in a queue directory; once a day you fold the queue in and open a PR.

Arguments: `queue=<dir>`. Every `*.md` directly in that directory is one candidate. `<queue>/processed/` is the archive of candidates already folded in — read it for context if you like, but never treat it as new input.

**The journal being wrong is worse than it being stale.** Every later run trusts it, so a confidently wrong entry propagates into comments posted to real reporters. One entry recently asserted a credential leak that had already been fixed; left alone it would have had a triage run tell a reporter to rotate keys that never leaked. Your job is as much deleting and correcting as adding.

## 1. Read the current journal

Read it end to end before touching anything. You need to know what is already covered — most candidates restate something the journal says, and the right outcome for those is to drop them, or to sharpen the existing entry rather than add a second one.

Note its own "Adding to this file" section: name the symbol rather than the line number, cite the issue number, keep it short.

## 2. Verify every candidate against current main

A candidate is a claim made during one investigation, at whatever commit was checked out then. Before it goes in:

```bash
git fetch origin main && git log --oneline -1 origin/main
```

For each candidate, check the claim still holds — grep the symbol it names, read the function, check whether a merge has since fixed or contradicted it. Then choose:

- **Fold it in** — place it in the right table row or section, rewritten in the journal's voice. Say what was verified and how.
- **Rewrite it** — the observation is real but the explanation was wrong or has been overtaken.
- **Drop it** — already covered, unverifiable, or fixed since. Dropping is a normal outcome; say so in the PR body with the reason.

Anything the candidate flagged as suspected rather than verified stays marked that way, or comes out. Do not upgrade a hypothesis to a fact because it reads well.

## 3. Re-check the journal against what has merged since

This is the half that keeps the file honest, and it is not optional.

```bash
git log --oneline --since="2 weeks ago" origin/main
```

Look for merges touching areas the journal makes claims about. For each, ask whether an existing entry is now wrong: a bug it calls "still live" that has been fixed, a line citation that has drifted onto unrelated code, a `confirmed on main (checked ...)` that a later merge has invalidated. Correct those in the same PR, and say plainly in the body which entries changed and why.

If a fix has landed, say so and keep the mechanism — "this was the bug, it was fixed in PR #N" is useful to a reader holding an older log. Do not simply delete the entry.

## 4. Spelling

New vendor and firmware terms will fail the cspell hook. Add genuine terms to `.cspell/custom-dictionary-workspace.txt`; reword ordinary English rather than adding it. The file is auto-sorted by a hook, so re-stage it after running pre-commit.

## 5. Quality gate

```bash
./run_pre_commit
```

Docs-only changes still have to pass cspell and markdownlint. Do not skip it.

## 6. Commit and open the PR

```bash
git checkout -b bot/debug-journal-<YYYY-MM-DD>
git add .claude/skills/issue-triage/references/debug-journal.md .cspell/custom-dictionary-workspace.txt
git commit -m "docs(debug-journal): <what changed>"
git push -u origin bot/debug-journal-<YYYY-MM-DD>
gh pr create --draft --title "..." --body-file <path>
```

The PR body must open with a line disclosing it is automated, then list, per candidate, what you folded in, rewrote or dropped **and why** — that list is what makes the PR reviewable in a couple of minutes instead of requiring a full re-read of the diff. Name every existing entry you corrected separately, with the merge that invalidated it.

Do not merge it. A human merge is the review gate on this file.

## 7. If there is nothing worth landing

If every candidate is a duplicate or fails verification, and nothing in the journal needs correcting, open no PR. Say so in your final message. An empty day is a perfectly good outcome and much better than a PR that adds noise to the file every other flow depends on.
