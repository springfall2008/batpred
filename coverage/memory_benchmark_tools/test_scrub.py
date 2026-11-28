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

    def test_byte_scans_skip_npz_files(self):
        """Compressed arrays can match a pattern by chance, so .npz files are left out of both byte scans."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "history.npz").write_bytes(b"\x00SECRET-999 jo@example.org\x01")
            (root / "same.json").write_text("SECRET-999")
            self.assertEqual(scrub.find_leaks(root, ["SECRET-999"]), ["same.json"])
            self.assertEqual(scrub.find_personal_data(root), [])

    def test_scan_text_reports_leaks_and_personal_data_by_kind(self):
        """scan_text checks a string the way the byte scans check a file, naming what it found by kind only."""
        self.assertEqual(scrub.scan_text('{"version": 1}', ["SECRET-999"], []), [])
        self.assertEqual(scrub.scan_text('{"note": "SECRET-999"}', ["SECRET-999"], []), ["real identifier"])
        self.assertEqual(scrub.scan_text('{"note": "jo@example.org"}', [], []), ["email"])

    def test_find_leaks_ignores_empty_secrets(self):
        """An empty secret would match every file, so it is ignored."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("text")
            self.assertEqual(scrub.find_leaks(root, ["", None]), [])


class TestPersonalData(unittest.TestCase):
    """Personal data is scrubbed by category, and anything that still looks personal is found."""

    def test_scrub_personal_attributes_replaces_location_and_contact_fields(self):
        """Coordinates become the fake ones and contact fields are blanked, at any depth; other attributes stay."""
        attributes = {
            "latitude": 52.1,
            "longitude": -1.6,
            "friendly_name": "Home",
            "data": {"first_name": "Jo", "surname": "X", "email": "jo@example.org", "address": "1 Road", "postcode": "AB1 2CD", "telephone_number": "07123456789", "company": "Co"},
        }
        result = scrub.scrub_personal_attributes(attributes)
        self.assertEqual((result["latitude"], result["longitude"]), (scrub.FAKE_LATITUDE, scrub.FAKE_LONGITUDE))
        self.assertEqual(result["friendly_name"], "Home")
        self.assertEqual(result["data"]["postcode"], scrub.FAKE_POSTCODE)
        for field in ("first_name", "surname", "email", "address", "telephone_number", "company"):
            self.assertEqual(result["data"][field], "benchmark", field)
        self.assertEqual(attributes["latitude"], 52.1)

    def test_name_id_tokens_finds_device_ids_in_entity_names(self):
        """Long numeric or mixed tokens in entity names are device ids; ordinary words and the fakes are not."""
        names = [
            "sensor.predbat_solax_1234567890123456789_h1234abcd5678_soc",
            "sensor.predbat_wallbox_123456_power",
            "event.octopus_energy_electricity_21e1234567_bench-mpan-1_current_day_rates",
            "sensor.predbat_octopus_a_bench0001_free_electricity",
            "sensor.predbat_pv_forecast_h0_uncalibrated",
            "input_number.predbat_pv_metric10_weight",
            "input_number.predbat_load_scaling10",
        ]
        found = scrub.name_id_tokens(names)
        self.assertEqual(set(found), {"1234567890123456789", "h1234abcd5678", "123456", "21e1234567"})
        self.assertEqual(sorted(found.values()), ["benchid1", "benchid2", "benchid3", "benchid4"])

    def test_find_leaks_ignores_case(self):
        """A real value is found whatever its case, as an integration may lower-case it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("event.octopus_energy_electricity_21e1234567_rates")
            self.assertEqual(scrub.find_leaks(root, ["21E1234567"]), ["a.txt"])

    def test_find_personal_data_skips_the_public_vehicle_catalogue(self):
        """Octopus's public vehicle catalogue holds model names shaped like postcodes (P90DL, S05 4WD); it is not personal data."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cache").mkdir()
            (root / "cache" / "octopus_vehicle_catalogue.json").write_text('{"models": ["P90DL", "S05 4WD"]}')
            self.assertEqual(scrub.find_personal_data(root), [])

    def test_find_personal_data_names_the_kind_only(self):
        """Emails, phone numbers, postcodes other than the fake and real coordinates are reported by kind, without the value."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clean.txt").write_text('{"latitude": 51.501, "longitude": -0.1419, "postcode": "SW1A 1AA"}')
            (root / "email.txt").write_text("contact jo@example.org")
            (root / "phone.txt").write_text("call 07123 456789")
            (root / "postcode.txt").write_text("lives at AB1 2CD")
            (root / "coords.txt").write_text('{"latitude": 52.123456}')
            self.assertEqual(scrub.find_personal_data(root), [("coords.txt", "coordinates"), ("email.txt", "email"), ("phone.txt", "phone"), ("postcode.txt", "postcode")])


if __name__ == "__main__":
    unittest.main()
