# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice resolve cross-language conformance test
# -----------------------------------------------------------------------------
"""Pins batpred's resolve to the language-neutral lattice-spec corpus (vendored)."""

import json
import os
import unittest

from lattice import resolve

_CORPUS = os.path.join(os.path.dirname(__file__), "tests", "lattice_resolve_corpus")

# The observable result of a resolution — the behaviour every language's resolver must agree on
# (mirrors editor/scripts/resolve-runner.mjs FIELDS).
_FIELDS = [
    "ok",
    "side",
    "node",
    "nodeKind",
    "chosenAccessPath",
    "fellBack",
    "reducer",
    "routeNodeCount",
    "strategy",
    "planNodes",
    "distribution",
    "ownedNodes",
    "ownershipNote",
    "unit",
    "shape",
    "tier",
    "controlGroup",
    "groupMembers",
    "derived",
    "clamped",
    "clampMin",
    "clampMaxLabel",
    "binding",
    "intent",
    "message",
]


def _project(result):
    """Keep the observable FIELD set, dropping absent keys — the cross-language comparison form."""
    return {k: result[k] for k in _FIELDS if k in result and result[k] is not None}


class TestResolveCorpus(unittest.TestCase):
    """Every corpus case must resolve to the golden projected result."""

    def test_corpus(self):
        """batpred resolve == the lattice-spec reference on all vendored cases."""
        with open(os.path.join(_CORPUS, "cases.json"), encoding="utf-8") as handle:
            cases = json.load(handle)
        with open(os.path.join(_CORPUS, "expected.json"), encoding="utf-8") as handle:
            expected = json.load(handle)
        self.assertEqual(len(cases), len(expected))
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertIn(case["name"], expected)
                query = case["query"]
                got = resolve(
                    case["doc"],
                    query["capability"],
                    query["side"],
                    query.get("intent"),
                    set(query.get("offline") or []),
                    query.get("altitude") or "auto",
                )
                self.assertEqual(_project(got), expected[case["name"]])


if __name__ == "__main__":
    unittest.main()
