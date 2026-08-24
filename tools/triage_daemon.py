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
then runs `claude -p "/issue-triage <number>"` (the skill at
.claude/skills/issue-triage/SKILL.md) with a permission mode that
denies anything not explicitly listed below - git/gh read commands,
running one targeted test, editing files only inside this dedicated
clone, and fetching issue attachments. All MCP tools are explicitly
denied (--disallowedTools "mcp__*"), and --max-turns/--max-budget-usd
cap a single invocation from running away.
"""

import json
import subprocess
import time
from pathlib import Path

REPO = "springfall2008/batpred"
BASE_DIR = Path.home() / "predbat-triage-bot"
CLONE_DIR = BASE_DIR / "batpred"
STATE_FILE = BASE_DIR / "state.json"
POLL_SECONDS = 300

EDIT_SCOPE = f"//{CLONE_DIR.relative_to('/')}/**"
ALLOWED_TOOLS = ",".join(
    [
        "Bash(gh *)",
        "Bash(git log*)",
        "Bash(git diff*)",
        "Bash(git show*)",
        "Bash(git blame*)",
        "Bash(git grep*)",
        "Bash(cd *)",
        "Bash(./run_all*)",
        f"Edit({EDIT_SCOPE})",
        "WebFetch",
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


def triage(issue_number):
    cmd = [
        "claude",
        "-p",
        f"/issue-triage {issue_number}",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        ALLOWED_TOOLS,
        "--disallowedTools",
        "mcp__*",
        "--max-turns",
        "40",
        # Client-side token-usage estimate, not a real spend cap under subscription
        # auth (see agent-sdk/cost-tracking) - just a circuit-breaker against a
        # runaway invocation, sized generously since one issue can need several
        # file reads plus a test run.
        "--max-budget-usd",
        "10.00",
    ]
    print(f"[triage] issue #{issue_number}: starting", flush=True)
    result = subprocess.run(cmd, cwd=str(CLONE_DIR))
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
                triage(issue["number"])
                state["last_processed"] = issue["number"]
                save_state(state)
        except subprocess.CalledProcessError as exc:
            print(f"[triage] error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
