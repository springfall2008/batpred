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

   Add --ollama <model> to run every 'claude' invocation against an Ollama
   model instead of the default Claude model, e.g.:

       python3 tools/triage_daemon.py --ollama glm-5.3-flash:cloud

   Or add --ollama_review <model> to use Ollama only for the read-only/
   review-ish flows - first-pass triage, follow-up review, PR review, PR
   cleanup - while PR creation (/issue-pr, the flow that actually writes
   the fix) still runs on the default Claude model:

       python3 tools/triage_daemon.py --ollama_review glm-5.3-flash:cloud

   --ollama and --ollama_review are mutually exclusive - --ollama already
   covers every flow --ollama_review does, plus PR creation, so pass at
   most one.

   Both point the CLI at Ollama's Claude Code compatible endpoint
   (https://docs.ollama.com/integrations/claude-code), served locally at
   OLLAMA_BASE_URL (http://localhost:11434 by default) - so it needs a
   local `ollama serve` running, and, for a :cloud-suffixed model, an
   `ollama signin` on this machine. --max-budget-usd is omitted entirely
   for whichever flows are running against Ollama (see claude_budget_args()) -
   its cost estimate is priced for Anthropic's API and was observed to fire
   falsely against an Ollama model regardless of the real (near-zero) spend
   (issue #4881); --max-turns is the limit that still holds for them.

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

import argparse
import json
import os
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
# Set from --ollama/--ollama_review by main() - see effective_ollama_model() for how the
# two interact. OLLAMA_BASE_URL is Ollama's own Claude Code compatible endpoint
# (https://docs.ollama.com/integrations/claude-code); a :cloud-suffixed model is still
# routed through it, forwarded using the machine's `ollama signin` credentials rather
# than a separate API key.
OLLAMA_MODEL = None
OLLAMA_REVIEW_MODEL = None
OLLAMA_BASE_URL = "http://localhost:11434"

EDIT_SCOPE = f"//{CLONE_DIR.relative_to('/')}/**"
SCRATCH_SCOPE = f"//{SCRATCH_DIR.relative_to('/')}/**"
_ALLOWED_TOOLS_NON_GH = [
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
# Read the issue, search for duplicates, post the triage comment
_ALLOWED_TOOLS_BASE = ["Bash(gh *)"] + _ALLOWED_TOOLS_NON_GH
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
#
# The "-f" entry needs a space on both sides of the flag, not "git push*-f*" - that
# unanchored form matches "-f" as a substring anywhere in the command, including
# inside a perfectly ordinary branch name. "git push -u origin
# fix/power-flow-car-outside-ct-clamp-4788" contains "-flow", which the old pattern
# read as a force-push flag and denied outright (issue #4788: the branch never made
# it past a manual push). Anchoring "-f" to its own token still catches "git push -f
# ...", "git push ... -f" and "--force"/"--force-with-lease", without also catching
# "-flow", "-fix", "-format" or any other word that merely contains "-f".
_PR_FORCE_PUSH_DENIALS = ["Bash(git push* --force*)", "Bash(git push* -f)", "Bash(git push* -f *)"]
DISALLOWED_TOOLS_PR = ",".join([item for item in _DISALLOWED_TOOLS_BASE if item not in _PR_REMOVED_DENIALS] + _PR_FORCE_PUSH_DENIALS)
# The review and cleanup flows do NOT inherit the broad "Bash(gh *)" grant: with it
# present, carving a scoped exception out of the gh api denial below would do nothing,
# since "Bash(gh *)" already allows every gh api call once that denial is lifted, and
# a narrower allow gets no precedence over a broader one - only "deny wins over allow"
# is a real rule here. Instead, list the specific gh subcommands actually needed, so
# there is no catch-all for the scoped gh api grant to hide behind.
_ALLOWED_GH_PR_READ = [
    "Bash(gh pr view*)",
    "Bash(gh pr diff*)",
    "Bash(gh pr list*)",
    "Bash(gh pr comment*)",
    "Bash(gh issue view*)",
    "Bash(gh issue list*)",
    "Bash(gh search*)",
]
# /code-review posts findings as inline PR comments, which needs gh api against the
# PR-comments endpoint - scoped to this repo only. No write/push/commit access, and
# deliberately no "gh pr review*" either: that would also allow --approve/
# --request-changes, a governance action beyond "post a comment."
_REVIEW_REMOVED_DENIALS = {"Bash(gh api*)"}
# Prefix-glob matching is literal, so a single "Bash(gh api repos/O/R/*)" rule only fires
# when the endpoint is the very next token, bare. Two forms the agent reaches for miss it
# and are denied outright under dontAsk: a quoted endpoint, and a method flag ahead of the
# endpoint - which is the canonical way to write a POST, and therefore exactly the form the
# comment-posting step picks. That is what silently reduced PR #4758's review to printed
# findings, while #4759's POST happened to be endpoint-first and went through. Enumerate the
# realistic (method flag, quoting) combinations instead. Only POST and PATCH are listed -
# the flow creates and edits comments, it never needs DELETE or PUT - and every variant stays
# pinned to this repo, so the extra forms widen the accepted spelling, not the reach.
# GH_API_ENDPOINT_FIRST_PROMPT below steers the agent onto the bare form, making this list a
# safety net for the spellings we did not think of rather than the primary mechanism.
_GH_API_METHOD_FLAGS = ["", "--method POST ", "--method PATCH ", "-X POST ", "-X PATCH "]
_GH_API_ENDPOINT_QUOTES = ["", '"', "'"]
_REVIEW_EXTRA_ALLOWED = [f"Bash(gh api {flag}{quote}repos/{REPO}/*)" for flag in _GH_API_METHOD_FLAGS for quote in _GH_API_ENDPOINT_QUOTES]
ALLOWED_TOOLS_REVIEW = ",".join(_ALLOWED_GH_PR_READ + _ALLOWED_TOOLS_NON_GH + _REVIEW_EXTRA_ALLOWED)
DISALLOWED_TOOLS_REVIEW = ",".join(item for item in _DISALLOWED_TOOLS_BASE if item not in _REVIEW_REMOVED_DENIALS)
# BOT_CLEANUP needs the review flow's read access and scoped gh api grant, plus
# committing/pushing/pre-commit and checking out the PR's own branch (the review flow
# never needs a local checkout, and never gh pr checks/run view - it doesn't touch CI).
# Deliberately not the full _ALLOWED_TOOLS_PR_EXTRA: no "gh pr create*" - cleanup
# pushes to the existing PR's branch, it never opens a new one. The merge grant below
# is what lets it sync a stale PR branch with main and resolve conflicts, per
# pr-cleanup/SKILL.md step 2 - no other flow checks out an existing branch that can
# be behind, so it's cleanup-only.
_CLEANUP_EXTRA_GH = ["Bash(gh pr checkout*)", "Bash(gh pr checks*)", "Bash(gh run view*)", "Bash(gh run list*)"]
# Enumerated spellings rather than a bare "git merge*" - unscoped, that would let the
# agent merge any ref, not just origin/main, contradicting pr-cleanup/SKILL.md's own
# guardrail ("Only ever merge origin/main into the branch, never any other ref"). One
# entry has to cover a flag ahead of the ref too: "git merge --no-edit origin/main"
# (SKILL.md's own example command, chosen to avoid hanging on an interactive editor
# prompt for the merge commit message) - prefix-glob matching is literal, so a bare
# "git merge origin/main*" rule would not match it, the same footgun already
# documented against _PR_FORCE_PUSH_DENIALS and the gh api endpoint-first form above.
_CLEANUP_EXTRA_MERGE = [
    "Bash(git merge origin/main*)",
    "Bash(git merge --no-edit origin/main*)",
    "Bash(git merge --abort)",
]
_CLEANUP_EXTRA_WRITE = [
    "Bash(git add*)",
    "Bash(git commit*)",
    "Bash(git push*)",
    "Bash(./run_pre_commit*)",
    "Bash(./run_pre_commit)",
]
ALLOWED_TOOLS_CLEANUP = ",".join(_ALLOWED_GH_PR_READ + _CLEANUP_EXTRA_GH + _ALLOWED_TOOLS_NON_GH + _CLEANUP_EXTRA_MERGE + _CLEANUP_EXTRA_WRITE + _REVIEW_EXTRA_ALLOWED)
_CLEANUP_REMOVED_DENIALS = {"Bash(git push*)", "Bash(git commit*)"} | _REVIEW_REMOVED_DENIALS
DISALLOWED_TOOLS_CLEANUP = ",".join([item for item in _DISALLOWED_TOOLS_BASE if item not in _CLEANUP_REMOVED_DENIALS] + _PR_FORCE_PUSH_DENIALS)
# The other half of the #4758 fix. /code-review is a built-in skill, so the command form it
# has to use cannot be pinned in a SKILL.md we own - it goes in as an appended system prompt
# on the two flows holding the scoped gh api grant. Belt and braces with _REVIEW_EXTRA_ALLOWED
# above: the allowlist covers the spellings we enumerated, this keeps the agent on the one
# spelling that is certain to be covered, and asks it to say so loudly when a call is denied
# anyway - #4758 quietly degraded to printing the comments it could not post, which reads like
# a finished review in the log. Also carries the bot-disclosure requirement for these two flows:
# /code-review's own instructions live in a skill we don't own, so this prompt is the only
# lever available for it; /pr-cleanup's SKILL.md already asks for disclosure directly, and
# this is the belt-and-braces backup for it, same reasoning as the endpoint-first steer.
GH_API_ENDPOINT_FIRST_PROMPT = (
    "Permission rules in this session match a literal command prefix, so `gh api` calls are only permitted when the current allowlist covers the exact spelling you use. "
    "Prefer the endpoint-first, unquoted form (endpoint immediately after `gh api`) and put flags after the endpoint - for example "
    f"`gh api repos/{REPO}/pulls/123/comments --method POST -f path=apps/predbat/example.py`. "
    'Other spellings (e.g. `gh api --method POST repos/...`, `gh api -X POST repos/...`, `gh api -H ... repos/...` or `gh api "repos/..."`) may be denied in restricted sessions even when the same request is allowed in endpoint-first form. '
    "Keep each call to a single command: piping into head/tail/grep is fine, but redirecting output anywhere outside "
    f"{SCRATCH_DIR} or the repository clone - /tmp included - is denied as well. "
    "If a call is denied regardless, state that plainly in your final message and name the command; do not quietly fall back "
    "to printing the comments you would have posted. "
    "Every comment or reply you post in this session - an inline review comment, a review-thread reply - must open with a "
    "short line disclosing it is automated, e.g. '_Automated comment from the triage bot._', so a maintainer can tell "
    "bot-authored feedback apart from a human reviewer's, without needing to check the author field."
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


def issue_url(issue_number):
    """Return the GitHub URL for an issue, for easy opening from the daemon's log."""
    return f"https://github.com/{REPO}/issues/{issue_number}"


def pr_url(pr_number):
    """Return the GitHub URL for a PR, for easy opening from the daemon's log."""
    return f"https://github.com/{REPO}/pull/{pr_number}"


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


def find_pr_number_for_issue(issue_number):
    """Return the number of the PR referencing this issue (matching the exact
    "Fixes #N" phrase /issue-pr always includes), or None if none exists yet.
    """
    query = build_duplicate_search_query(issue_number)
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO, "--search", query, "--state", "all", "--json", "number", "--limit", "100"],
        capture_output=True,
        text=True,
        check=True,
    )
    prs = json.loads(result.stdout)
    return prs[0]["number"] if prs else None


def has_existing_pr(issue_number):
    """Return True if a PR already references this issue, in any state."""
    return find_pr_number_for_issue(issue_number) is not None


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


def flag_pr_for_review(pr_number):
    """Add BOT_REVIEW to a PR, so the next poll cycle runs /code-review against it.
    Idempotent - adding a label the PR already carries is a no-op, not an error.
    """
    subprocess.run(["gh", "pr", "edit", str(pr_number), "--repo", REPO, "--add-label", "BOT_REVIEW"], check=True)


def mark_pr_opened(issue_number):
    """Swap BOT_PR for BOT_PR_OPENED once the draft PR has been confirmed open, and
    flag the PR itself with BOT_REVIEW so a code review runs against it automatically -
    /issue-pr's own quality gate (step 4 of its SKILL.md) is pre-commit and a targeted
    test, not an LLM review of the diff.
    """
    subprocess.run(
        ["gh", "issue", "edit", str(issue_number), "--repo", REPO, "--remove-label", "BOT_PR", "--add-label", "BOT_PR_OPENED"],
        check=True,
    )
    pr_number = find_pr_number_for_issue(issue_number)
    if pr_number is not None:
        flag_pr_for_review(pr_number)


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
            "Automated PR creation skipped: `BOT_PR` was added but this issue isn't actionable for an automated implementation " "(closed, or not classified `bug`/`enhancement`). Remove `BOT_PR_FAILED` and re-add `BOT_PR` once the classification changes.",
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


def effective_ollama_model(review_only=False):
    """Return the Ollama model this claude invocation should use, or None to use the
    default Claude model. --ollama (OLLAMA_MODEL) always wins and applies to every
    invocation, including PR creation. --ollama_review (OLLAMA_REVIEW_MODEL) applies
    only when the caller passes review_only=True - triage(), triage_followup(),
    review_pr() and cleanup_pr() do; create_pr() never does, so PR creation still runs
    on the full Claude model even when --ollama_review is set.
    """
    if OLLAMA_MODEL:
        return OLLAMA_MODEL
    if review_only and OLLAMA_REVIEW_MODEL:
        return OLLAMA_REVIEW_MODEL
    return None


def claude_model_args(review_only=False):
    """Return the extra 'claude' CLI args selecting the Ollama model for this
    invocation, or [] to use the default Claude model. Appended to every claude
    invocation's cmd list below.
    """
    model = effective_ollama_model(review_only)
    return ["--model", model] if model else []


def claude_env(review_only=False):
    """Return the subprocess environment for a 'claude' invocation: None (inherit
    the daemon's own environment unchanged) unless this invocation is using an
    Ollama model, in which case add the Anthropic-compatible overrides Ollama's
    Claude Code integration documents, so the CLI talks to the local Ollama server
    instead of Anthropic's API.
    """
    model = effective_ollama_model(review_only)
    if not model:
        return None
    env = os.environ.copy()
    env["ANTHROPIC_BASE_URL"] = OLLAMA_BASE_URL
    env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
    env["ANTHROPIC_API_KEY"] = ""
    return env


def claude_budget_args(amount, review_only=False):
    """Return the extra 'claude' CLI args capping spend at `amount` USD, or [] when
    this invocation is running against an Ollama model. --max-budget-usd's cost
    estimate is priced for Anthropic's API, and has been observed to fire falsely
    against an Ollama model regardless: issue #4881's first triage attempt completed
    its real work (comment posted, BOT_TRIAGED applied) and then kept running until
    the estimate crossed $10, aborting with a non-zero exit that made the daemon
    retry an already-finished issue. --max-turns is the circuit-breaker that still
    applies in Ollama mode.
    """
    if effective_ollama_model(review_only):
        return []
    return ["--max-budget-usd", amount]


def triage(issue_number):
    cmd = (
        [
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
        ]
        + claude_model_args(review_only=True)
        + claude_budget_args("10.00", review_only=True)
    )
    log_path = LOG_DIR / f"issue-{issue_number}.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[triage] issue #{issue_number}: starting, logging to {log_path}", flush=True)
    # Append rather than truncate: a failed run leaves the issue unprocessed, so the
    # next poll retries it - and the failed attempt's output is the part worth keeping.
    with log_path.open("a") as log_handle:
        log_handle.write(f"\n==== issue #{issue_number} started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
        log_handle.flush()
        result = subprocess.run(cmd, cwd=str(CLONE_DIR), stdout=log_handle, stderr=subprocess.STDOUT, env=claude_env(review_only=True))
        log_handle.write(f"==== issue #{issue_number} exited {result.returncode} ====\n")
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    print(f"[triage] issue #{issue_number}: exited {result.returncode}", flush=True)


def triage_followup(issue_number):
    """Run the /issue-triage-followup skill for one issue - a re-review incorporating
    information added since the original triage, under the same triage permission
    set as first-pass /issue-triage (no commits, pushes, or PR creation).
    """
    cmd = (
        [
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
        ]
        + claude_model_args(review_only=True)
        + claude_budget_args("10.00", review_only=True)
    )
    log_path = LOG_DIR / f"issue-{issue_number}-followup.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[review] issue #{issue_number}: starting follow-up, logging to {log_path}", flush=True)
    with log_path.open("a") as log_handle:
        log_handle.write(f"\n==== issue #{issue_number} follow-up started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
        log_handle.flush()
        result = subprocess.run(cmd, cwd=str(CLONE_DIR), stdout=log_handle, stderr=subprocess.STDOUT, env=claude_env(review_only=True))
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
    cmd = (
        [
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
        ]
        + claude_model_args()
        + claude_budget_args("25.00")
    )
    log_path = LOG_DIR / f"issue-{issue_number}-pr.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[pr] issue #{issue_number}: starting, logging to {log_path}", flush=True)
    with log_path.open("a") as log_handle:
        log_handle.write(f"\n==== issue #{issue_number} PR flow started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
        log_handle.flush()
        result = subprocess.run(cmd, cwd=str(CLONE_DIR), stdout=log_handle, stderr=subprocess.STDOUT, env=claude_env())
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


def fetch_bot_review_prs():
    """Return open PRs currently labelled BOT_REVIEW, each with its title."""
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO, "--state", "open", "--label", "BOT_REVIEW", "--json", "number,title", "--limit", "100"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def fetch_bot_cleanup_prs():
    """Return open PRs currently labelled BOT_CLEANUP, each with its title."""
    result = subprocess.run(
        ["gh", "pr", "list", "--repo", REPO, "--state", "open", "--label", "BOT_CLEANUP", "--json", "number,title", "--limit", "100"],
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


def remove_pr_review_label(pr_number):
    """Remove BOT_REVIEW from a PR once the review has been posted, so it isn't reprocessed."""
    subprocess.run(["gh", "pr", "edit", str(pr_number), "--repo", REPO, "--remove-label", "BOT_REVIEW"], check=True)


def mark_pr_review_failed(pr_number, reason=""):
    """Post a note and swap BOT_REVIEW for BOT_FAILED on a PR, so a failing review isn't
    retried every poll cycle. Remove BOT_FAILED and re-add BOT_REVIEW to retry. `reason`
    names the specific failure when there is one - "see the logs" is poor advice for the
    run that exits 0 having posted nothing, because its log reads like a finished review.
    """
    detail = f" {reason}" if reason else ""
    subprocess.run(
        [
            "gh",
            "pr",
            "comment",
            str(pr_number),
            "--repo",
            REPO,
            "--body",
            f"Automated review failed to complete for this PR - see the triage bot's logs for details.{detail} " "Not retrying automatically; remove `BOT_FAILED` and re-add `BOT_REVIEW` to try again.",
        ],
        check=True,
    )
    subprocess.run(
        ["gh", "pr", "edit", str(pr_number), "--repo", REPO, "--remove-label", "BOT_REVIEW", "--add-label", "BOT_FAILED"],
        check=True,
    )


def remove_pr_cleanup_label(pr_number):
    """Remove BOT_CLEANUP once fixes have been committed and pushed, so it isn't reprocessed."""
    subprocess.run(["gh", "pr", "edit", str(pr_number), "--repo", REPO, "--remove-label", "BOT_CLEANUP"], check=True)


def mark_pr_cleanup_failed(pr_number):
    """Post a note and swap BOT_CLEANUP for BOT_FAILED on a PR, so a failing cleanup
    isn't retried every poll cycle. Remove BOT_FAILED and re-add BOT_CLEANUP to retry.
    """
    subprocess.run(
        [
            "gh",
            "pr",
            "comment",
            str(pr_number),
            "--repo",
            REPO,
            "--body",
            "Automated cleanup failed to complete for this PR - see the triage bot's logs for details. " "Not retrying automatically; remove `BOT_FAILED` and re-add `BOT_CLEANUP` to try again.",
        ],
        check=True,
    )
    subprocess.run(
        ["gh", "pr", "edit", str(pr_number), "--repo", REPO, "--remove-label", "BOT_CLEANUP", "--add-label", "BOT_FAILED"],
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


def review_pr(pr_number):
    """Run /code-review against a PR at the "high" effort level, posting findings as
    inline PR comments. Read-only otherwise: no code changes, no push, no PR actions.
    """
    cmd = (
        [
            "claude",
            "-p",
            f"/code-review {pr_number} high --comment",
            "--append-system-prompt",
            GH_API_ENDPOINT_FIRST_PROMPT,
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            ALLOWED_TOOLS_REVIEW,
            "--disallowedTools",
            DISALLOWED_TOOLS_REVIEW,
            "--verbose",
            "--add-dir",
            str(SCRATCH_DIR),
            "--max-turns",
            "100",
        ]
        + claude_model_args(review_only=True)
        + claude_budget_args("20.00", review_only=True)
    )
    log_path = LOG_DIR / f"pr-{pr_number}-review.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[review-pr] PR #{pr_number}: starting, logging to {log_path}", flush=True)
    with log_path.open("a") as log_handle:
        log_handle.write(f"\n==== PR #{pr_number} review started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
        log_handle.flush()
        result = subprocess.run(cmd, cwd=str(CLONE_DIR), stdout=log_handle, stderr=subprocess.STDOUT, env=claude_env(review_only=True))
        log_handle.write(f"==== PR #{pr_number} review exited {result.returncode} ====\n")
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    print(f"[review-pr] PR #{pr_number}: exited {result.returncode}", flush=True)


def pr_review_activity_count(pr_number):
    """Return how many review bodies sit on a PR: submitted reviews, inline review
    comments and plain PR comments, summed. process_bot_review_pr() samples this before
    and after a run, because the exit status cannot answer the question that matters -
    `claude -p` exits 0 whether or not the posting step was actually permitted, so the
    count moving is the only available evidence that a review landed.
    """
    total = 0
    for endpoint in (f"repos/{REPO}/pulls/{pr_number}/reviews", f"repos/{REPO}/pulls/{pr_number}/comments", f"repos/{REPO}/issues/{pr_number}/comments"):
        result = subprocess.run(["gh", "api", endpoint, "--paginate", "--jq", "length"], capture_output=True, text=True, check=True)
        total += sum(int(page) for page in result.stdout.split())
    return total


def process_bot_review_pr(pr):
    """Run the BOT_REVIEW flow for one PR: /code-review posts findings as comments,
    nothing here ever touches the PR's code. On success, remove BOT_REVIEW - the
    posted review is the artifact, there's no separate "done" state to track. A
    failed invocation swaps to BOT_FAILED instead, with an explanatory comment, as
    does a run that exits 0 without posting anything: PR #4758's review had every
    inline comment denied by the permission rules, still exited 0, and had BOT_REVIEW
    cleared - leaving no review, no BOT_FAILED, and nothing marking it for retry.
    """
    pr_number = pr["number"]
    print(f'[review-pr] PR #{pr_number}: "{pr["title"]}" - {pr_url(pr_number)}', flush=True)
    sync_repo()
    reset_scratch()

    try:
        before = pr_review_activity_count(pr_number)
    except subprocess.CalledProcessError as exc:
        print(f"[review-pr] PR #{pr_number}: failed to sample activity count before review: {exc}", flush=True)
        mark_pr_review_failed(pr_number, "Unable to sample PR review activity before running the review, so the result could not be verified.")
        return

    try:
        review_pr(pr_number)
    except subprocess.CalledProcessError as exc:
        print(f"[review-pr] PR #{pr_number}: review failed: {exc}", flush=True)
        mark_pr_review_failed(pr_number)
        return

    try:
        after = pr_review_activity_count(pr_number)
    except subprocess.CalledProcessError as exc:
        print(f"[review-pr] PR #{pr_number}: failed to sample activity count after review: {exc}", flush=True)
        mark_pr_review_failed(pr_number, "The review run finished, but the activity count check failed, so it could not be verified that anything was posted.")
        return

    if after <= before:
        print(f"[review-pr] PR #{pr_number}: exited cleanly but posted nothing, marking failed", flush=True)
        mark_pr_review_failed(pr_number, "The run exited cleanly but posted nothing, so the review step itself did not complete.")
        return
    remove_pr_review_label(pr_number)


def cleanup_pr(pr_number):
    """Run the /pr-cleanup skill against a PR: address review feedback and CI
    failures, then commit and push - under the write-capable cleanup permission set.
    """
    cmd = (
        [
            "claude",
            "-p",
            f"/pr-cleanup {pr_number} scratch={SCRATCH_DIR}",
            "--append-system-prompt",
            GH_API_ENDPOINT_FIRST_PROMPT,
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            ALLOWED_TOOLS_CLEANUP,
            "--disallowedTools",
            DISALLOWED_TOOLS_CLEANUP,
            "--verbose",
            "--add-dir",
            str(SCRATCH_DIR),
            "--max-turns",
            "150",
        ]
        + claude_model_args(review_only=True)
        + claude_budget_args("25.00", review_only=True)
    )
    log_path = LOG_DIR / f"pr-{pr_number}-cleanup.log"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[cleanup-pr] PR #{pr_number}: starting, logging to {log_path}", flush=True)
    with log_path.open("a") as log_handle:
        log_handle.write(f"\n==== PR #{pr_number} cleanup started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
        log_handle.flush()
        result = subprocess.run(cmd, cwd=str(CLONE_DIR), stdout=log_handle, stderr=subprocess.STDOUT, env=claude_env(review_only=True))
        log_handle.write(f"==== PR #{pr_number} cleanup exited {result.returncode} ====\n")
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    print(f"[cleanup-pr] PR #{pr_number}: exited {result.returncode}", flush=True)


def process_bot_cleanup_pr(pr):
    """Run the BOT_CLEANUP flow for one PR: address review feedback and CI failures,
    then remove the trigger label. A failed run swaps to BOT_FAILED instead, with an
    explanatory comment.
    """
    pr_number = pr["number"]
    print(f'[cleanup-pr] PR #{pr_number}: "{pr["title"]}" - {pr_url(pr_number)}', flush=True)
    sync_repo()
    reset_scratch()
    try:
        cleanup_pr(pr_number)
    except subprocess.CalledProcessError as exc:
        print(f"[cleanup-pr] PR #{pr_number}: cleanup failed: {exc}", flush=True)
        mark_pr_cleanup_failed(pr_number)
        return
    remove_pr_cleanup_label(pr_number)


def parse_args():
    """Parse the daemon's CLI arguments - --ollama and --ollama_review, to run
    'claude' invocations against an Ollama model instead of the default Claude model.
    """
    parser = argparse.ArgumentParser(description="Poll batpred issues/PRs and triage them with Claude Code.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--ollama",
        metavar="MODEL",
        help="Run every 'claude' invocation against an Ollama model served at "
        f"{OLLAMA_BASE_URL} instead of the default Claude model - e.g. --ollama glm-5.3-flash:cloud. "
        "Uses Ollama's Claude Code compatible endpoint (https://docs.ollama.com/integrations/claude-code); "
        "a :cloud-suffixed model is still routed through the local Ollama server, forwarded using this "
        "machine's `ollama signin` credentials rather than a separate API key.",
    )
    group.add_argument(
        "--ollama_review",
        metavar="MODEL",
        help="Like --ollama, but only for the read-only/review-ish flows - first-pass " "triage, follow-up review, PR review, PR cleanup. PR creation (/issue-pr, the " "flow that writes the actual fix) still runs on the default Claude model.",
    )
    return parser.parse_args()


def main():
    global OLLAMA_MODEL, OLLAMA_REVIEW_MODEL
    args = parse_args()
    OLLAMA_MODEL = args.ollama
    OLLAMA_REVIEW_MODEL = args.ollama_review

    if not CLONE_DIR.exists():
        raise SystemExit(f"Expected a git clone at {CLONE_DIR} - see setup steps before running this daemon.")
    if OLLAMA_MODEL:
        print(f"[triage] using Ollama model {OLLAMA_MODEL!r} via {OLLAMA_BASE_URL} for every claude invocation", flush=True)
    elif OLLAMA_REVIEW_MODEL:
        print(f"[triage] using Ollama model {OLLAMA_REVIEW_MODEL!r} via {OLLAMA_BASE_URL} for review flows only (PR creation still uses Claude)", flush=True)

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
            for pr in fetch_bot_review_prs():
                process_bot_review_pr(pr)
            for pr in fetch_bot_cleanup_prs():
                process_bot_cleanup_pr(pr)
        except subprocess.CalledProcessError as exc:
            print(f"[triage] error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
