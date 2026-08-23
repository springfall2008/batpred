#!/usr/bin/env python3
"""Polls springfall2008/batpred for new GitHub issues and triages each one
with Claude Code, using a dedicated local clone kept in sync with main.
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
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_processed": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


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
        "--max-budget-usd",
        "2.00",
    ]
    print(f"[triage] issue #{issue_number}: starting", flush=True)
    result = subprocess.run(cmd, cwd=str(CLONE_DIR))
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
