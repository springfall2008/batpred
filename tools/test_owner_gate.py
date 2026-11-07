#!/usr/bin/env python3
"""Tests for the owner gate workflow script (.github/workflows/owner-gate.yml).

Runs the job's shell script exactly as it appears in the workflow, under bash, against
a fake `gh` that serves the PR from a JSON file and applies the
script's writes to it. Needs bash and jq on PATH, as the GitHub runner has. Run
directly with `python3 tools/test_owner_gate.py`; nothing here touches GitHub.
"""

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "owner-gate.yml"
OWNER = "springfall2008"
LABEL = "owner-approved"
RUN_URL = "https://github.com/springfall2008/batpred/actions/runs/1"

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
    # Fail the next N calls of a kind, standing in for an API error
    kind = "status" if "/statuses/" in endpoint else "mutation" if "mutation" in fields.get("query", "") else method
    if st["fail"].get(kind, 0) > 0:
        st["fail"][kind] -= 1
        json.dump(st, open(path, "w"))
        sys.exit("fake gh: HTTP 502 for " + kind)
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
    elif "/statuses/" in endpoint:
        # gh api sends a POST when given fields
        if len(fields["description"]) > 140:
            sys.exit("fake gh: HTTP 422 description is too long")
        st["statuses"].append(dict(fields, sha=endpoint.rsplit("/", 1)[1]))
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


def make_status(state, sha="sha1"):
    """Build an owner-approval status already on a commit."""
    return {"sha": sha, "context": "owner-approval", "state": state, "description": "", "target_url": RUN_URL}


def labelled_by(login):
    """Build an issue event recording who applied the owner-approved label."""
    return {"event": "labeled", "label": {"name": LABEL}, "actor": {"login": login}}


def approval_of(sha):
    """Build the owner's approving review of a commit."""
    return {"author": {"login": OWNER}, "state": "APPROVED", "commit": {"oid": sha}}


class OwnerGateTests(unittest.TestCase):
    """Run the workflow's script against a fake GitHub and check the owner-approval status it leaves."""

    @classmethod
    def setUpClass(cls):
        """Extract the script from the workflow and write the fake gh and a no-op sleep."""
        if not shutil.which("bash") or not shutil.which("jq"):
            raise unittest.SkipTest("tools/test_owner_gate.py requires bash and jq on PATH")
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

    def run_gate(self, pr, statuses=(), events=(), changes=(), fail=None, action="labeled", sender=OWNER):
        """Run the script once for an event and return the resulting state."""
        path = Path(self.tmp.name) / "state.json"
        state = {"pr": pr, "statuses": list(statuses), "events": list(events), "changes": list(changes), "fail": fail or {}, "calls": [], "reads": 0}
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
            RUN_URL=RUN_URL,
        )
        # The runner's default shell for a run step is bash -e
        result = subprocess.run(["bash", "-e", "-c", self.script], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        state = json.loads(path.read_text())
        state["output"] = result.stdout
        return state

    def gate(self, state):
        """Return the status that counts: the newest owner-approval status on the head commit."""
        head = state["pr"]["headRefOid"]
        return [s for s in state["statuses"] if s["sha"] == head and s["context"] == "owner-approval"][-1]

    def test_first_run_posts_a_pending_status(self):
        """With no go-ahead, the run posts one pending status linking to its own log."""
        state = self.run_gate(make_pr())
        self.assertEqual(len(state["statuses"]), 1)
        self.assertEqual(self.gate(state)["state"], "pending")
        self.assertEqual(self.gate(state)["target_url"], RUN_URL)

    def test_go_ahead_posts_success(self):
        """The owner enabling auto-merge posts a newer success over the pending status."""
        state = self.run_gate(make_pr(auto_merge_by=OWNER), statuses=[make_status("pending")], action="auto_merge_enabled")
        self.assertEqual([s["state"] for s in state["statuses"]], ["pending", "success"])

    def test_withdrawal_posts_pending_over_success(self):
        """Switching auto-merge off posts a newer pending status over the success."""
        state = self.run_gate(make_pr(), statuses=[make_status("success")], action="auto_merge_disabled")
        self.assertEqual(self.gate(state)["state"], "pending")

    def test_failed_status_post_is_retried(self):
        """A single failed status post is retried."""
        state = self.run_gate(make_pr(auto_merge_by=OWNER), fail={"status": 1}, action="auto_merge_enabled")
        self.assertEqual(self.gate(state)["state"], "success")
        self.assertNotIn("::warning::", state["output"])

    def test_status_post_failing_twice_warns_without_failing(self):
        """If the status cannot be posted at all, the run warns and still succeeds."""
        state = self.run_gate(make_pr(), fail={"status": 2})
        self.assertEqual(state["statuses"], [])
        self.assertIn("::warning::Could not record the owner-approval status", state["output"])

    def test_long_message_is_cut_to_fit(self):
        """A message over GitHub's 140-character limit is cut down rather than rejected."""
        pr = make_pr(auto_merge_by=OWNER, labels=[LABEL])
        state = self.run_gate(pr, events=[labelled_by(OWNER)], fail={"DELETE": 1, "mutation": 1}, action="synchronize", sender="a-collaborator-with-a-long-name")
        self.assertEqual(self.gate(state)["state"], "pending")
        self.assertEqual(len(self.gate(state)["description"]), 140)

    def test_failed_revocation_still_records_waiting(self):
        """A revocation that fails still records a pending status, warns, and does not fail the run."""
        pr = make_pr(auto_merge_by=OWNER, labels=[LABEL])
        state = self.run_gate(pr, events=[labelled_by(OWNER)], fail={"DELETE": 1, "mutation": 1}, action="synchronize", sender="collaborator")
        self.assertEqual(self.gate(state)["state"], "pending")
        self.assertIn("the owner-approved label and auto-merge could not be removed", self.gate(state)["description"])
        self.assertIn("::warning::Could not remove the owner-approved label and auto-merge", state["output"])

    def test_go_ahead_landing_mid_run_is_recorded(self):
        """A run that read the PR just before auto-merge was enabled corrects its waiting verdict."""
        changes = [{"on_read": 2, "pr": {"autoMergeRequest": {"enabledBy": {"login": OWNER}}}}]
        state = self.run_gate(make_pr(), changes=changes)
        self.assertEqual([s["state"] for s in state["statuses"]], ["pending", "success"])

    def test_withdrawal_landing_mid_run_is_recorded(self):
        """A run that read the PR just before auto-merge was switched off corrects its pass."""
        changes = [{"on_read": 2, "pr": {"autoMergeRequest": None}}]
        state = self.run_gate(make_pr(auto_merge_by=OWNER), changes=changes, action="auto_merge_enabled")
        self.assertEqual(self.gate(state)["state"], "pending")

    def test_new_head_mid_run_is_left_to_its_own_run(self):
        """When the head moves mid-run, the run stops and records nothing on the new commit."""
        changes = [{"on_read": 2, "pr": {"headRefOid": "sha2", "autoMergeRequest": {"enabledBy": {"login": OWNER}}}}]
        state = self.run_gate(make_pr(), changes=changes)
        self.assertEqual([s["sha"] for s in state["statuses"]], ["sha1"])

    def test_non_owner_push_revokes_the_go_ahead(self):
        """A collaborator's push removes the label, switches auto-merge off and records waiting."""
        state = self.run_gate(make_pr(auto_merge_by=OWNER, labels=[LABEL]), events=[labelled_by(OWNER)], action="synchronize", sender="collaborator")
        self.assertEqual(state["pr"]["labels"]["nodes"], [])
        self.assertIsNone(state["pr"]["autoMergeRequest"])
        self.assertEqual(self.gate(state)["state"], "pending")
        self.assertIn("revoked", self.gate(state)["description"])

    def test_owner_push_keeps_the_go_ahead(self):
        """The owner's own push does not revoke the label."""
        state = self.run_gate(make_pr(labels=[LABEL]), events=[labelled_by(OWNER)], action="synchronize")
        self.assertEqual(self.gate(state)["state"], "success")
        self.assertTrue(state["pr"]["labels"]["nodes"])

    def test_label_applied_by_a_collaborator_does_not_count(self):
        """Only the owner applying the label, most recently, counts."""
        state = self.run_gate(make_pr(labels=[LABEL]), events=[labelled_by(OWNER), labelled_by("collaborator")])
        self.assertEqual(self.gate(state)["state"], "pending")
        self.assertIn("does not count", state["output"])

    def test_owner_approval_counts_only_for_the_head_commit(self):
        """The owner's approving review passes for the head commit but not an earlier one."""
        state = self.run_gate(make_pr(reviews=[approval_of("sha1")]), action="submitted")
        self.assertEqual(self.gate(state)["state"], "success")
        state = self.run_gate(make_pr(reviews=[approval_of("sha0")]), action="submitted")
        self.assertEqual(self.gate(state)["state"], "pending")


if __name__ == "__main__":
    unittest.main()
