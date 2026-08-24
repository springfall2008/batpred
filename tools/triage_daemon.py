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
ALLOWED_TOOLS = ",".join(
    [
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
        # Running one targeted test, from coverage/ or from the repo root
        "Bash(cd *)",
        "Bash(./run_all*)",
        "Bash(coverage/run_all*)",
        "Bash(./coverage/run_all*)",
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
        f"Edit({EDIT_SCOPE})",
        f"Edit({SCRATCH_SCOPE})",
        f"Write({EDIT_SCOPE})",
        f"Write({SCRATCH_SCOPE})",
        "WebFetch",
        "Read",
        "Grep",
        "Glob",
    ]
)
# Deny wins over allow, so these carve the publishing commands back out of
# the broad "Bash(gh *)" / "Bash(git ...)" entries above.
DISALLOWED_TOOLS = ",".join(
    [
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
    ]
)


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


def fetch_new_issues(since_number):
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--state", "open", "--json", "number,createdAt", "--limit", "100"],
        capture_output=True,
        text=True,
        check=True,
    )
    issues = json.loads(result.stdout)
    new = [i for i in issues if i["number"] > since_number]
    return sorted(new, key=lambda i: i["number"])


def sync_repo():
    subprocess.run(["git", "-C", str(CLONE_DIR), "fetch", "origin", "main"], check=True)
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


def main():
    if not CLONE_DIR.exists():
        raise SystemExit(f"Expected a git clone at {CLONE_DIR} - see setup steps before running this daemon.")

    state = load_state()
    while True:
        try:
            for issue in fetch_new_issues(state["last_processed"]):
                sync_repo()
                reset_scratch()
                triage(issue["number"])
                state["last_processed"] = issue["number"]
                save_state(state)
        except subprocess.CalledProcessError as exc:
            print(f"[triage] error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
