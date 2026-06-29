# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice merge cross-language conformance test
# -----------------------------------------------------------------------------
"""Pins batpred's merge to the language-neutral lattice-spec corpus (vendored, normalized)."""

import json
import os
import unittest

from lattice import merge

_CORPUS = os.path.join(os.path.dirname(__file__), "tests", "lattice_merge_corpus")


def _normalize(result):
    """Return {site, warnings} with the implementation-defined site.docVersion removed."""
    site = dict(result["site"])
    site.pop("docVersion", None)
    return {"site": site, "warnings": result["warnings"]}


class TestMergeCorpus(unittest.TestCase):
    """Every corpus case must merge to the golden {site, warnings} (docVersion normalized out)."""

    def test_corpus(self):
        """batpred merge == the lattice-spec reference on all vendored cases."""
        with open(os.path.join(_CORPUS, "cases.json"), encoding="utf-8") as handle:
            cases = json.load(handle)
        with open(os.path.join(_CORPUS, "expected.json"), encoding="utf-8") as handle:
            expected = json.load(handle)
        self.assertEqual(len(cases), len(expected))
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertIn(case["name"], expected)
                self.assertEqual(_normalize(merge(case["inputs"])), expected[case["name"]])


if __name__ == "__main__":
    unittest.main()
