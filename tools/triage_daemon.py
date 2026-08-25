#!/usr/bin/env python3
"""Polls springfall2008/batpred for new GitHub issues and triages each one
with Claude Code, using a dedicated local clone kept in sync with main.

Setup (one-time, on whichever always-on machine will run this):

1. Update Claude Code first: `claude update`. The --max-budget-usd cap
   below only enforces spend on v2.1.217+; older versions accept the
   flag but won't actually cap anything.

2. Clone a copy of the repo dedicated to this daemon - do NOT point it
   at a machine's normal working copy. Each run does a hard reset to
   origin/main before invoking Claude, which would blow away any
   in-progress work in a real dev checkout.

       mkdir -p ~/predbat-triage-bot
       cd ~/predbat-triage-bot
       git clone https://github.com/springfall2008/batpred

3. Set up the test venv once, so the skill can run targeted tests:

       cd ~/predbat-triage-bot/batpred/coverage
       source setup.csh

4. Seed the state file with the CURRENT highest open issue number, so
   the first run doesn't triage the entire existing backlog:

       gh issue list --repo springfall2008/batpred --state open \
           --json number --limit 200 \
           | python3 -c "import json,sys; print(max(i['number'] for i in json.load(sys.stdin)))"

       echo '{"last_processed": <number from above>}' > ~/predbat-triage-bot/state.json

5. Run it. It polls every 5 minutes and keeps running until stopped
   (Ctrl-C, or run it under nohup/tmux/a LaunchAgent to survive a
   closed terminal):

       python3 tools/triage_daemon.py

What it does per new issue: syncs the dedicated clone to origin/main,
empties the scratch directory used for issue attachments, then runs
`claude -p "/issue-triage <number> scratch=<dir>"` (the skill at
.claude/skills/issue-triage/SKILL.md) with a permission mode that denies
anything not explicitly listed in ALLOWED_TOOLS: gh, git history and
re-sync, running one targeted test, downloading and grepping issue
attachments (logs are routinely far larger than WebFetch will return),
and editing files inside the clone or the scratch directory.
DISALLOWED_TOOLS blocks all MCP tools plus the git/gh commands that
would publish something, and --max-turns/--max-budget-usd cap a single
invocation from running away.

Everything Claude prints for an issue is captured to
~/predbat-triage-bot/logs/issue-<number>.log - appended, not truncated, so a
run that failed and got retried keeps the failed attempt too. The daemon's own
console output just says which issue it is working on and where that log is;
`tail -f` it to watch a triage in progress. Logs are never pruned, so clear the
directory out yourself if it grows.

The allowlist deliberately includes general-purpose tools (python3,
curl, ./run_all), which together amount to arbitrary code execution
inside the clone - the triage skill needs to open a reporter's log,
parse their debug yaml and run a test. That is what the dedicated
throwaway clone in step 2 is protecting: treat this checkout as
disposable, and don't run the daemon on a machine holding credentials
you wouldn't hand to an issue reporter. DISALLOWED_TOOLS is a backstop
against the obvious mistakes, not a sandbox boundary.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

REPO = "springfall2008/batpred"
BASE_DIR = Path.home() / "predbat-triage-bot"
CLONE_DIR = BASE_DIR / "batpred"
SCRATCH_DIR = BASE_DIR / "scratch"
LOG_DIR = BASE_DIR / "logs"
STATE_FILE = BASE_DIR / "state.json"
POLL_SECONDS = 300

EDIT_SCOPE = f"//{CLONE_DIR.relative_to('/')}/**"
SCRATCH_SCOPE = f"//{SCRATCH_DIR.relative_to('/')}/**"
_ALLOWED_TOOLS_BASE = [
    # Read the issue, search for duplicates, post the triage comment
    "Bash(gh *)",
    # Git history, and the re-sync/discard the skill does before investigating
    "Bash(git log*)",
    "Bash(git diff*)",
    "Bash(git show*)",
    "Bash(git blame*)",
    "Bash(git grep*)",
    "Bash(git status*)",
    "Bash(git branch*)",
    "Bash(git tag*)",
    "Bash(git describe*)",
    "Bash(git rev-parse*)",
    "Bash(git remote -v*)",
    "Bash(git remote show*)",
    "Bash(git fetch*)",
    "Bash(git reset*)",
    "Bash(git checkout*)",
    "Bash(git restore*)",
    "Bash(git clean*)",
    "Bash(git stash*)",
    # Running one targeted test. tools/triage_test.sh keeps the cd into
    # coverage/ and the output redirect inside the script, because Claude Code
    # prompts on a cd combined with an output redirect - which is what the
    # obvious "cd coverage && ./run_all --test X > log 2>&1" form is, so it is
    # denied outright under dontAsk no matter what rules are listed here.
    "Bash(tools/triage_test.sh *)",
    "Bash(./tools/triage_test.sh *)",
    "Bash(cd *)",
    "Bash(./run_all *)",
    "Bash(./run_all)",
    "Bash(python3 *)",
    # Pulling issue attachments down and picking them apart. A predbat.log
    # is often tens of MB - too big for WebFetch and too big to read into
    # context, so it gets downloaded and grepped instead.
    "Bash(curl *)",
    "Bash(mkdir *)",
    "Bash(unzip *)",
    "Bash(gunzip *)",
    "Bash(zcat *)",
    "Bash(tar *)",
    "Bash(file *)",
    "Bash(ls *)",
    "Bash(find *)",
    "Bash(wc *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(cat *)",
    "Bash(grep *)",
    "Bash(rg *)",
    "Bash(sed *)",
    "Bash(awk *)",
    "Bash(sort *)",
    "Bash(uniq *)",
    "Bash(cut *)",
    "Bash(tr *)",
    "Bash(tee *)",
    "Bash(echo *)",
    # Edit rules cover every file-editing tool (Write included) - a Write(path)
    # rule is not matched by the file permission check, so don't add one.
    f"Edit({EDIT_SCOPE})",
    f"Edit({SCRATCH_SCOPE})",
    "WebFetch",
    "Read",
    "Grep",
    "Glob",
]
ALLOWED_TOOLS = ",".join(_ALLOWED_TOOLS_BASE)
# The /issue-pr invocation needs everything the read-only triage flow has, plus
# committing/pushing its branch, opening the PR, and running pre-commit as a quality gate.
_ALLOWED_TOOLS_PR_EXTRA = [
    "Bash(git add*)",
    "Bash(git commit*)",
    "Bash(git push*)",
    "Bash(gh pr create*)",
    "Bash(./run_pre_commit*)",
    "Bash(./run_pre_commit)",
]
ALLOWED_TOOLS_PR = ",".join(_ALLOWED_TOOLS_BASE + _ALLOWED_TOOLS_PR_EXTRA)
# Deny wins over allow, so these carve the publishing commands back out of
# the broad "Bash(gh *)" / "Bash(git ...)" entries above.
_DISALLOWED_TOOLS_BASE = [
    "mcp__*",
    "Bash(git push*)",
    "Bash(git commit*)",
    "Bash(git remote add*)",
    "Bash(git remote set-url*)",
    "Bash(gh pr create*)",
    "Bash(gh pr merge*)",
    "Bash(gh pr close*)",
    "Bash(gh repo*)",
    "Bash(gh release*)",
    "Bash(gh workflow*)",
    "Bash(gh auth*)",
    "Bash(gh secret*)",
    "Bash(gh api*)",
]
DISALLOWED_TOOLS = ",".join(_DISALLOWED_TOOLS_BASE)
# The PR flow needs to push its branch and open the PR - carve those three back out of
# the base denial list. Everything else (merge, close, repo/release/workflow/auth/secret/
# api admin, all mcp__*) stays denied.
_PR_REMOVED_DENIALS = {"Bash(git push*)", "Bash(git commit*)", "Bash(gh pr create*)"}
# Even though the PR flow can push, force-push variants stay denied - defense in depth
# against a prompt-injected instruction attempting to rewrite history. Prefix-glob
# matching can't parse flags, so this is a heuristic, not a guarantee.
_PR_FORCE_PUSH_DENIALS = ["Bash(git push*--force*)", "Bash(git push*-f*)"]
DISALLOWED_TOOLS_PR = ",".join([item for item in _DISALLOWED_TOOLS_BASE if item not in _PR_REMOVED_DENIALS] + _PR_FORCE_PUSH_DENIALS)


def load_state():
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except json.JSONDecodeError:
        return {"last_processed": 0}
    return {"last_processed": 0}


def save_state(state):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state))
    tmp_path.replace(STATE_FILE)


def issue_url(issue_number):
    """Return the GitHub URL for an issue, for easy opening from the daemon's log."""
    return f"https://github.com/{REPO}/issues/{issue_number}"


def fetch_new_issues(since_number):
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--state", "open", "--json", "number,createdAt,title", "--limit", "100"],
        capture_output=True,
        text=True,
        check=True,
    )
    issues = json.loads(result.stdout)
    new = [i for i in issues if i["number"] > since_number]
    return sorted(new, key=lambda i: i["number"])


def fetch_bot_pr_issues():
    """Return open issues currently labelled BOT_PR, each with its full label list."""
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--state", "open", "--label", "BOT_PR", "--json", "number,labels,title", "--limit", "100"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


TRIAGE_DISCLOSURE_MARKER = "automated first-pass triage"


def is_already_triaged(labels):
    """Return True if BOT_TRIAGED is present in a gh --json labels list."""
    return any(label["name"] == "BOT_TRIAGED" for label in labels)


def find_triage_comment(issue_number):
    """Return True if the issue already carries a bot triage comment (pre-BOT_TRIAGED-label issues)."""
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--repo", REPO, "--json", "comments"],
        capture_output=True,
        text=True,
        check=True,
    )
    comments = json.loads(result.stdout).get("comments", [])
    return any(TRIAGE_DISCLOSURE_MARKER in comment.get("body", "") for comment in comments)


def backfill_triaged_label(issue_number):
    """Apply BOT_TRIAGED to an issue found to already have a bot triage comment."""
    subprocess.run(["gh", "issue", "edit", str(issue_number), "--repo", REPO, "--add-label", "BOT_TRIAGED"], check=True)


def ensure_triaged(issue_number, labels):
    """Return True if the issue is (now) marked BOT_TRIAGED; False if /issue-triage still needs to run.

    Checks the label first, then falls back to scanning comments for issues triaged
    before BOT_TRIAGED existed, backfilling the label onto them when found.
    """
    if is_already_triaged(labels):
        return True
    if find_triage_comment(issue_number):
        backfill_triaged_label(issue_number)
        return True
    return False


def build_duplicate_search_query(issue_number):
    """Build the gh pr list --search query used to detect an existing PR for an issue.

    Matches the exact "Fixes #N" phrase the issue-pr skill always includes in its PR
    body, rather than a bare issue number, which could match an unrelated PR.
    """
    return f'"Fixes #{issue_number}" in:body'


def has_existing_pr(issue_number):
    """Return True if a PR already references this issue, in any state."""
    query = build_duplicate_search_query(issue_number)
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO, "--search", query, "--state", "all", "--json", "number"],
        capture_output=True,
        text=True,
        check=True,
    )
    return len(json.loads(result.stdout)) > 0


def is_actionable(issue_number):
    """Return True if the issue is open and classified bug or enhancement - the only
    classifications /issue-pr should attempt to implement. Guards against an inline
    /issue-triage call classifying the ticket as a duplicate (and closing it), a
    question, or a configuration issue, which create_pr() must never run against.
    """
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--repo", REPO, "--json", "state,labels"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    if data["state"] != "OPEN":
        return False
    label_names = {label["name"] for label in data["labels"]}
    return bool(label_names & {"bug", "enhancement"})


def mark_pr_opened(issue_number):
    """Swap BOT_PR for BOT_PR_OPENED once the draft PR has been confirmed open."""
    subprocess.run(
        ["gh", "issue", "edit", str(issue_number), "--repo", REPO, "--remove-label", "BOT_PR", "--add-label", "BOT_PR_OPENED"],
        check=True,
    )


def mark_pr_failed(issue_number):
    """Swap BOT_PR for BOT_PR_FAILED so a failed run isn't retried every poll cycle."""
    subprocess.run(
        ["gh", "issue", "edit", str(issue_number), "--repo", REPO, "--remove-label", "BOT_PR", "--add-label", "BOT_PR_FAILED"],
        check=True,
    )


def mark_pr_not_actionable(issue_number):
    """Post a note explaining why no PR was attempted, then reuse mark_pr_failed for
    the label swap - the issue turned out closed, or not classified bug/enhancement.
    """
    subprocess.run(
        [
            "gh",
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            REPO,
            "--body",
            "`BOT_PR` was added but this issue isn't actionable for an automated implementation " "(closed, or not classified `bug`/`enhancement`) - not attempting a PR. Remove `BOT_PR_FAILED` " "and re-add `BOT_PR` once the classification changes.",
        ],
        check=True,
    )
    mark_pr_failed(issue_number)


def sync_repo():
    """Sync the clone to origin/main, always returning to main first.

    A crashed BOT_PR run can leave the clone checked out on a fix/*|feat/* branch;
    without an explicit checkout, reset --hard would reset that branch instead of
    main, leaving the clone stuck off main for every subsequent operation.
    """
    subprocess.run(["git", "-C", str(CLONE_DIR), "fetch", "origin", "main"], check=True)
    subprocess.run(["git", "-C", str(CLONE_DIR), "checkout", "main"], check=True)
    subprocess.run(["git", "-C", str(CLONE_DIR), "reset", "--hard", "origin/main"], check=True)
    # Drop untracked leftovers from the previous run's investigation. Not -x:
    # coverage/venv/ is gitignored and expensive to rebuild every issue.
    subprocess.run(["git", "-C", str(CLONE_DIR), "clean", "-fd"], check=True)


def reset_scratch():
    shutil.rmtree(SCRATCH_DIR, ignore_errors=True)
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)


def triage(issue_number):
    cmd = [
        "claude",
        "-p",
        f"/issue-triage {issue_number} scratch={SCRATCH_DIR}",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        ALLOWED_TOOLS,
        "--disallowedTools",
        DISALLOWED_TOOLS,
        # Turn-by-turn trace rather than just the final message, so the per-issue
        # log below shows which tool calls ran and which were denied.
        "--verbose",
        # Downloads land outside the clone, so the session needs the scratch
        # directory in scope for Read/Grep as well as for the Bash rules above.
        "--add-dir",
        str(SCRATCH_DIR),
        "--max-turns",
        "60",
        # Client-side token-usage estimate, not a real spend cap under subscription
        # auth (see agent-sdk/cost-tracking) - just a circuit-breaker against a
        # runaway invocation, sized generously since one issue can need several
        # file reads plus a test run.
        "--max-budget-usd",
        "10.00",
    ]
    log_path = LOG_DIR / f"issue-{issue_number}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[triage] issue #{issue_number}: starting, logging to {log_path}", flush=True)
    # Append rather than truncate: a failed run leaves the issue unprocessed, so the
    # next poll retries it - and the failed attempt's output is the part worth keeping.
    with log_path.open("a") as log_handle:
        log_handle.write(f"\n==== issue #{issue_number} started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
        log_handle.flush()
        result = subprocess.run(cmd, cwd=str(CLONE_DIR), stdout=log_handle, stderr=subprocess.STDOUT)
        log_handle.write(f"==== issue #{issue_number} exited {result.returncode} ====\n")
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    print(f"[triage] issue #{issue_number}: exited {result.returncode}", flush=True)


def triage_followup(issue_number):
    """Run the /issue-triage-followup skill for one issue - a re-review incorporating
    information added since the original triage, under the same triage permission
    set as first-pass /issue-triage (no commits, pushes, or PR creation).
    """
    cmd = [
        "claude",
        "-p",
        f"/issue-triage-followup {issue_number} scratch={SCRATCH_DIR}",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        ALLOWED_TOOLS,
        "--disallowedTools",
        DISALLOWED_TOOLS,
        "--verbose",
        "--add-dir",
        str(SCRATCH_DIR),
        "--max-turns",
        "60",
        "--max-budget-usd",
        "10.00",
    ]
    log_path = LOG_DIR / f"issue-{issue_number}-followup.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[review] issue #{issue_number}: starting follow-up, logging to {log_path}", flush=True)
    with log_path.open("a") as log_handle:
        log_handle.write(f"\n==== issue #{issue_number} follow-up started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
        log_handle.flush()
        result = subprocess.run(cmd, cwd=str(CLONE_DIR), stdout=log_handle, stderr=subprocess.STDOUT)
        log_handle.write(f"==== issue #{issue_number} follow-up exited {result.returncode} ====\n")
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    print(f"[review] issue #{issue_number}: follow-up exited {result.returncode}", flush=True)


def create_pr(issue_number):
    """Run the /issue-pr skill for one issue under the push/PR-create-capable permission set.

    Whether it succeeded is judged by the caller re-checking has_existing_pr()
    afterwards, not by this function's return value or the invocation's exit code -
    a claude -p session that completes normally exits 0 whether it opened a PR or
    decided the quality gate failed and posted a comment instead.
    """
    cmd = [
        "claude",
        "-p",
        f"/issue-pr {issue_number} scratch={SCRATCH_DIR}",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        ALLOWED_TOOLS_PR,
        "--disallowedTools",
        DISALLOWED_TOOLS_PR,
        "--verbose",
        "--add-dir",
        str(SCRATCH_DIR),
        "--max-turns",
        "150",
        "--max-budget-usd",
        "25.00",
    ]
    log_path = LOG_DIR / f"issue-{issue_number}-pr.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[pr] issue #{issue_number}: starting, logging to {log_path}", flush=True)
    with log_path.open("a") as log_handle:
        log_handle.write(f"\n==== issue #{issue_number} PR flow started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
        log_handle.flush()
        result = subprocess.run(cmd, cwd=str(CLONE_DIR), stdout=log_handle, stderr=subprocess.STDOUT)
        log_handle.write(f"==== issue #{issue_number} PR flow exited {result.returncode} ====\n")
    print(f"[pr] issue #{issue_number}: exited {result.returncode}", flush=True)


def process_bot_pr_issue(issue):
    """Run the full BOT_PR flow for one issue: guard against duplicate work, ensure it's
    triaged and actionable, run the PR flow, then swap the label based on whether a PR
    now exists.
    """
    issue_number = issue["number"]
    labels = issue.get("labels", [])
    print(f'[pr] issue #{issue_number}: "{issue["title"]}" - {issue_url(issue_number)}', flush=True)
    if has_existing_pr(issue_number):
        print(f"[pr] issue #{issue_number}: a PR already references this issue, marking opened", flush=True)
        mark_pr_opened(issue_number)
        return
    sync_repo()
    reset_scratch()
    if not ensure_triaged(issue_number, labels):
        triage(issue_number)
    if not is_actionable(issue_number):
        print(f"[pr] issue #{issue_number}: not actionable (closed, or not classified bug/enhancement), skipping", flush=True)
        mark_pr_not_actionable(issue_number)
        return
    create_pr(issue_number)
    if has_existing_pr(issue_number):
        mark_pr_opened(issue_number)
    else:
        mark_pr_failed(issue_number)


def fetch_bot_review_issues():
    """Return open issues currently labelled BOT_REVIEW, each with its full label list."""
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--state", "open", "--label", "BOT_REVIEW", "--json", "number,labels,title", "--limit", "100"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def remove_review_label(issue_number):
    """Remove BOT_REVIEW once the issue is confirmed triaged, so it isn't reprocessed."""
    subprocess.run(["gh", "issue", "edit", str(issue_number), "--repo", REPO, "--remove-label", "BOT_REVIEW"], check=True)


def mark_review_failed(issue_number):
    """Post a note and swap BOT_REVIEW for BOT_FAILED, so a failing triage isn't
    retried (and re-billed) every poll cycle. Remove BOT_FAILED and re-add BOT_REVIEW
    to retry once the underlying issue is fixed.
    """
    subprocess.run(
        [
            "gh",
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            REPO,
            "--body",
            "Automated triage failed to complete for this issue - see the triage bot's logs for details. " "Not retrying automatically; remove `BOT_FAILED` and re-add `BOT_REVIEW` to try again.",
        ],
        check=True,
    )
    subprocess.run(
        ["gh", "issue", "edit", str(issue_number), "--repo", REPO, "--remove-label", "BOT_REVIEW", "--add-label", "BOT_FAILED"],
        check=True,
    )


def process_bot_review_issue(issue):
    """Run the right BOT_REVIEW action for one issue, then remove the trigger label.

    Three cases: an issue already carrying BOT_TRIAGED gets a follow-up review via
    /issue-triage-followup, incorporating anything new since the original triage. An
    old, pre-BOT_TRIAGED-label issue that already has a bot triage comment is just
    backfilled with the label - catching up on backlog isn't the same as new
    information having arrived, so no follow-up runs for it. Anything else gets a
    first-pass /issue-triage, for issues older than the daemon's last_processed
    pointer, which the new-issue poll never sees.

    A failed triage or follow-up swaps BOT_REVIEW for BOT_FAILED instead of leaving
    BOT_REVIEW in place, so a persistently failing issue isn't retried every poll cycle.
    """
    issue_number = issue["number"]
    labels = issue.get("labels", [])
    print(f'[review] issue #{issue_number}: "{issue["title"]}" - {issue_url(issue_number)}', flush=True)

    if is_already_triaged(labels):
        action_name, action = "follow-up", triage_followup
    elif find_triage_comment(issue_number):
        backfill_triaged_label(issue_number)
        remove_review_label(issue_number)
        return
    else:
        action_name, action = "first-pass triage", triage

    sync_repo()
    reset_scratch()
    try:
        action(issue_number)
    except subprocess.CalledProcessError as exc:
        print(f"[review] issue #{issue_number}: {action_name} failed: {exc}", flush=True)
        mark_review_failed(issue_number)
        return
    remove_review_label(issue_number)


def main():
    if not CLONE_DIR.exists():
        raise SystemExit(f"Expected a git clone at {CLONE_DIR} - see setup steps before running this daemon.")

    state = load_state()
    while True:
        try:
            for issue in fetch_new_issues(state["last_processed"]):
                print(f'[triage] issue #{issue["number"]}: "{issue["title"]}" - {issue_url(issue["number"])}', flush=True)
                sync_repo()
                reset_scratch()
                triage(issue["number"])
                state["last_processed"] = issue["number"]
                save_state(state)
            for issue in fetch_bot_pr_issues():
                process_bot_pr_issue(issue)
            for issue in fetch_bot_review_issues():
                process_bot_review_issue(issue)
        except subprocess.CalledProcessError as exc:
            print(f"[triage] error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
