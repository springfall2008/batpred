# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for the memory benchmark's scrub helpers. Run: python3 -m unittest discover -s coverage/memory_benchmark_tools"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import scrub  # noqa: E402

ACCOUNT_CACHE = {
    "account_data": {
        "account": {
            "electricityAgreements": [{"meterPoint": {"mpan": "999", "meters": [{"serialNumber": "S1", "smartDevices": [{"deviceId": "30-EB-5A-FF"}]}], "agreements": []}}],
            "gasAgreements": [{"meterPoint": {"mprn": "888", "meters": [{"serialNumber": "G1"}]}}],
        }
    },
    "kraken_token": "tok",
    "note": "billed to account A-1234ABCD",
}


class TestCollectSensitive(unittest.TestCase):
    """collect_sensitive finds every identifier in the Octopus account cache."""

    def test_collect_sensitive_finds_account_mpan_mprn_and_serials(self):
        """The account number, MPAN, MPRN, meter serials and device IDs map to numbered fakes in order of appearance."""
        found = scrub.collect_sensitive(ACCOUNT_CACHE)
        self.assertEqual(
            found,
            {"999": "BENCH-MPAN-1", "S1": "BENCH-MSN-1", "30-EB-5A-FF": "BENCH-DEVICE-1", "888": "BENCH-MPRN-1", "G1": "BENCH-MSN-2", "A-1234ABCD": scrub.FAKE_ACCOUNT},
        )

    def test_collect_sensitive_never_takes_the_agreement_keys_for_identifiers(self):
        """The keys under account_data.account are the agreement lists, not the account number, and must survive."""
        found = scrub.collect_sensitive(ACCOUNT_CACHE)
        self.assertNotIn("electricityAgreements", found)
        self.assertNotIn("gasAgreements", found)

    def test_collect_sensitive_on_an_empty_cache(self):
        """An empty or missing account section gives no replacements."""
        self.assertEqual(scrub.collect_sensitive({}), {})


class TestReplaceEverywhere(unittest.TestCase):
    """replace_everywhere rewrites keys and string values without touching its input."""

    def test_replace_everywhere_covers_keys_and_values_and_leaves_input_untouched(self):
        """Keys, values and substrings inside longer strings are all replaced; the input is unchanged."""
        data = {"A-1234": {"url": "https://x/accounts/A-1234/meters/999/", "n": 3, "list": ["999", None, 1.5]}}
        result = scrub.replace_everywhere(data, {"A-1234": "A-FAKE", "999": "M-FAKE"})
        self.assertEqual(result, {"A-FAKE": {"url": "https://x/accounts/A-FAKE/meters/M-FAKE/", "n": 3, "list": ["M-FAKE", None, 1.5]}})
        self.assertIn("A-1234", data)
        self.assertEqual(data["A-1234"]["list"][0], "999")

    def test_replace_everywhere_prefers_longer_values(self):
        """A value that contains another is replaced whole, not by its shorter part."""
        result = scrub.replace_everywhere("ab abc", {"ab": "X", "abc": "Y"})
        self.assertEqual(result, "X Y")


class TestFindLeaks(unittest.TestCase):
    """find_leaks searches every file under a directory as bytes."""

    def test_find_leaks_finds_a_secret_in_a_binary_file_and_not_in_clean_ones(self):
        """A secret inside a binary file is reported by relative path; clean files are not."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            (root / "sub" / "db.bin").write_bytes(b"\x00\x01SECRET-999\x02")
            (root / "clean.txt").write_text("nothing here")
            self.assertEqual(scrub.find_leaks(root, ["SECRET-999", "unused"]), ["sub/db.bin"])

    def test_find_leaks_ignores_empty_secrets(self):
        """An empty secret would match every file, so it is ignored."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("text")
            self.assertEqual(scrub.find_leaks(root, ["", None]), [])


if __name__ == "__main__":
    unittest.main()
