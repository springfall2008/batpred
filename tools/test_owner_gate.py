#!/usr/bin/env python3
"""Tests for the owner gate workflow script (.github/workflows/owner-gate.yml).

Runs the job's shell script exactly as it appears in the workflow, under bash, against
a fake `gh` that serves the PR and its check runs from a JSON file and applies the
script's writes to it. Needs bash and jq on PATH, as the GitHub runner has. Run
directly with `python3 tools/test_owner_gate.py`; nothing here touches GitHub.
"""

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "owner-gate.yml"
OWNER = "springfall2008"
LABEL = "owner-approved"

# Stands in for `gh api`: reads state from $FAKE_STATE, applies writes, and pipes the
# response through jq when --jq is given, as gh does
FAKE_GH = textwrap.dedent(
    '''\
    #!/usr/bin/env python3
    """Fake gh api for the owner gate tests."""
    import json, os, subprocess, sys

    path = os.environ["FAKE_STATE"]
    st = json.load(open(path))
    args = sys.argv[2:]
    method, jq, fields, endpoint = "GET", None, {}, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-X":
            method = args[i + 1]
            i += 2
        elif a == "--jq":
            jq = args[i + 1]
            i += 2
        elif a in ("-f", "-F"):
            key, _, value = args[i + 1].partition("=")
            fields[key] = value
            i += 2
        elif a == "--paginate":
            i += 1
        else:
            endpoint = a
            i += 1
    st["calls"].append(method + " " + endpoint)
    out = {}
    if endpoint == "graphql":
        if "mutation" in fields["query"]:
            st["pr"]["autoMergeRequest"] = None
        else:
            st["reads"] += 1
            # A scripted change lands on the Nth read, standing in for another event mid-run
            for change in st["changes"]:
                if change["on_read"] == st["reads"]:
                    st["pr"].update(change["pr"])
            out = {"data": {"repository": {"pullRequest": st["pr"]}}}
    elif endpoint.endswith("/events"):
        out = st["events"]
    elif "/check-runs?" in endpoint:
        out = {"check_runs": [c for c in st["checks"] if c["name"] == "owner-gate"]}
    elif method == "PATCH":
        check = next(c for c in st["checks"] if c["id"] == int(endpoint.rsplit("/", 1)[1]))
        check.update(status=fields["status"], conclusion=fields.get("conclusion"), title=fields["output[title]"])
    elif method == "POST" and endpoint.endswith("/check-runs"):
        st["next_id"] += 1
        st["checks"].append({"id": st["next_id"], "name": fields["name"], "head_sha": fields["head_sha"], "status": fields["status"],
                             "conclusion": fields.get("conclusion"), "title": fields["output[title]"], "app": {"slug": "github-actions"}})
    elif method == "DELETE" and "/labels/" in endpoint:
        label = endpoint.rsplit("/", 1)[1]
        st["pr"]["labels"]["nodes"] = [n for n in st["pr"]["labels"]["nodes"] if n["name"] != label]
    else:
        sys.exit("fake gh: unhandled " + method + " " + endpoint)
    json.dump(st, open(path, "w"))
    text = json.dumps(out)
    if jq:
        sys.stdout.write(subprocess.run(["jq", "-r", jq], input=text, capture_output=True, text=True, check=True).stdout)
    else:
        print(text)
    '''
)


def make_pr(auto_merge_by=None, labels=(), reviews=(), head="sha1"):
    """Build the pullRequest object the script's GraphQL query returns."""
    return {
        "id": "PR_1",
        "headRefOid": head,
        "autoMergeRequest": {"enabledBy": {"login": auto_merge_by}} if auto_merge_by else None,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "latestOpinionatedReviews": {"nodes": list(reviews)},
    }


def make_check(check_id, status, conclusion=None, head="sha1", app="github-actions"):
    """Build an existing owner-gate check run on a commit."""
    return {"id": check_id, "name": "owner-gate", "head_sha": head, "status": status, "conclusion": conclusion, "title": "", "app": {"slug": app}}


def labelled_by(login):
    """Build an issue event recording who applied the owner-approved label."""
    return {"event": "labeled", "label": {"name": LABEL}, "actor": {"login": login}}


def approval_of(sha):
    """Build the owner's approving review of a commit."""
    return {"author": {"login": OWNER}, "state": "APPROVED", "commit": {"oid": sha}}


class OwnerGateTests(unittest.TestCase):
    """Run the workflow's script against a fake GitHub and check the owner-gate check it leaves."""

    @classmethod
    def setUpClass(cls):
        """Extract the script from the workflow and write the fake gh and a no-op sleep."""
        workflow = yaml.safe_load(WORKFLOW.read_text())
        cls.script = workflow["jobs"]["evaluate"]["steps"][0]["run"]
        cls.tmp = tempfile.TemporaryDirectory()
        bin_dir = Path(cls.tmp.name) / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH)
        (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
        for tool in ("gh", "sleep"):
            (bin_dir / tool).chmod(0o755)
        cls.bin_dir = bin_dir

    @classmethod
    def tearDownClass(cls):
        """Remove the temporary directory."""
        cls.tmp.cleanup()

    def run_gate(self, pr, checks=(), events=(), changes=(), action="labeled", sender=OWNER):
        """Run the script once for an event and return the resulting state."""
        path = Path(self.tmp.name) / "state.json"
        state = {"pr": pr, "checks": list(checks), "events": list(events), "changes": list(changes), "calls": [], "reads": 0, "next_id": 100}
        path.write_text(json.dumps(state))
        env = dict(
            os.environ,
            PATH=f"{self.bin_dir}:{os.environ['PATH']}",
            FAKE_STATE=str(path),
            GITHUB_REPOSITORY_OWNER=OWNER,
            GH_REPO=f"{OWNER}/batpred",
            PR_NUMBER="1",
            SENDER=sender,
            ACTION=action,
            LABEL=LABEL,
        )
        # The runner's default shell for a run step is bash -e
        result = subprocess.run(["bash", "-e", "-c", self.script], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads(path.read_text())
        state["output"] = result.stdout
        return state

    def gate(self, state):
        """Return the check that counts: the newest owner-gate check on the head commit."""
        head = state["pr"]["headRefOid"]
        return max((c for c in state["checks"] if c["head_sha"] == head), key=lambda c: c["id"])

    def test_first_run_creates_one_waiting_check(self):
        """With no go-ahead and no check yet, one in-progress check is created."""
        state = self.run_gate(make_pr())
        self.assertEqual(len(state["checks"]), 1)
        self.assertEqual(self.gate(state)["status"], "in_progress")

    def test_waiting_again_updates_the_waiting_check(self):
        """A second waiting verdict updates the in-progress check rather than adding another."""
        state = self.run_gate(make_pr(), checks=[make_check(5, "in_progress")])
        self.assertEqual([c["id"] for c in state["checks"]], [5])
        self.assertIn("PATCH repos/springfall2008/batpred/check-runs/5", state["calls"])

    def test_go_ahead_completes_the_waiting_check(self):
        """The owner enabling auto-merge completes the in-progress check as a success."""
        state = self.run_gate(make_pr(auto_merge_by=OWNER), checks=[make_check(5, "in_progress")], action="auto_merge_enabled")
        self.assertEqual(len(state["checks"]), 1)
        self.assertEqual((self.gate(state)["status"], self.gate(state)["conclusion"]), ("completed", "success"))

    def test_newest_of_several_waiting_checks_is_completed(self):
        """A commit carrying several waiting checks from before has its newest one completed."""
        checks = [make_check(5, "in_progress"), make_check(6, "in_progress"), make_check(7, "in_progress")]
        state = self.run_gate(make_pr(auto_merge_by=OWNER), checks=checks, action="auto_merge_enabled")
        self.assertEqual(self.gate(state)["id"], 7)
        self.assertEqual(self.gate(state)["conclusion"], "success")

    def test_completed_check_is_never_reopened(self):
        """Withdrawing the go-ahead adds a newer waiting check and leaves the completed one alone."""
        state = self.run_gate(make_pr(), checks=[make_check(5, "completed", "success")], action="auto_merge_disabled")
        self.assertEqual(state["checks"][0]["conclusion"], "success")
        self.assertEqual(len(state["checks"]), 2)
        self.assertEqual(self.gate(state)["status"], "in_progress")

    def test_another_apps_check_is_left_alone(self):
        """An owner-gate check from another app is never the one updated."""
        checks = [make_check(5, "in_progress"), make_check(9, "in_progress", app="someone-else")]
        state = self.run_gate(make_pr(auto_merge_by=OWNER), checks=checks, action="auto_merge_enabled")
        by_id = {c["id"]: c for c in state["checks"]}
        self.assertEqual(by_id[5]["conclusion"], "success")
        self.assertEqual(by_id[9]["status"], "in_progress")

    def test_go_ahead_landing_mid_run_is_recorded(self):
        """A run that read the PR just before auto-merge was enabled corrects its waiting verdict."""
        changes = [{"on_read": 2, "pr": {"autoMergeRequest": {"enabledBy": {"login": OWNER}}}}]
        state = self.run_gate(make_pr(), changes=changes)
        self.assertEqual(self.gate(state)["conclusion"], "success")
        self.assertEqual(len(state["checks"]), 1)

    def test_withdrawal_landing_mid_run_is_recorded(self):
        """A run that read the PR just before auto-merge was switched off corrects its pass."""
        changes = [{"on_read": 2, "pr": {"autoMergeRequest": None}}]
        state = self.run_gate(make_pr(auto_merge_by=OWNER), changes=changes, action="auto_merge_enabled")
        self.assertEqual(self.gate(state)["status"], "in_progress")

    def test_new_head_mid_run_is_left_to_its_own_run(self):
        """When the head moves mid-run, the run stops and records nothing on the new commit."""
        changes = [{"on_read": 2, "pr": {"headRefOid": "sha2", "autoMergeRequest": {"enabledBy": {"login": OWNER}}}}]
        state = self.run_gate(make_pr(), changes=changes)
        self.assertEqual([c["head_sha"] for c in state["checks"]], ["sha1"])

    def test_non_owner_push_revokes_the_go_ahead(self):
        """A collaborator's push removes the label, switches auto-merge off and records waiting."""
        state = self.run_gate(make_pr(auto_merge_by=OWNER, labels=[LABEL]), events=[labelled_by(OWNER)], action="synchronize", sender="collaborator")
        self.assertEqual(state["pr"]["labels"]["nodes"], [])
        self.assertIsNone(state["pr"]["autoMergeRequest"])
        self.assertEqual(self.gate(state)["status"], "in_progress")
        self.assertIn("revoked", self.gate(state)["title"])

    def test_owner_push_keeps_the_go_ahead(self):
        """The owner's own push does not revoke the label."""
        state = self.run_gate(make_pr(labels=[LABEL]), events=[labelled_by(OWNER)], action="synchronize")
        self.assertEqual(self.gate(state)["conclusion"], "success")
        self.assertTrue(state["pr"]["labels"]["nodes"])

    def test_label_applied_by_a_collaborator_does_not_count(self):
        """Only the owner applying the label, most recently, counts."""
        state = self.run_gate(make_pr(labels=[LABEL]), events=[labelled_by(OWNER), labelled_by("collaborator")])
        self.assertEqual(self.gate(state)["status"], "in_progress")
        self.assertIn("does not count", state["output"])

    def test_owner_approval_counts_only_for_the_head_commit(self):
        """The owner's approving review passes for the head commit but not an earlier one."""
        state = self.run_gate(make_pr(reviews=[approval_of("sha1")]), action="submitted")
        self.assertEqual(self.gate(state)["conclusion"], "success")
        state = self.run_gate(make_pr(reviews=[approval_of("sha0")]), action="submitted")
        self.assertEqual(self.gate(state)["status"], "in_progress")


if __name__ == "__main__":
    unittest.main()
