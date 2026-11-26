# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Scrub helpers for the memory benchmark fixture.

The fixture is built from a real Predbat install, so the identifiers in it (the Octopus account number, meter
serials, keys, the home's location) are replaced with fixed fakes before anything is written, and find_leaks
checks afterwards that none of the real values survived anywhere.
"""

import os
import re
from pathlib import Path
from typing import Iterable

FAKE_ACCOUNT = "A-BENCH0001"
FAKE_OCTOPUS_KEY = "sk_live_benchmark"
FAKE_FORECAST_KEY = "benchmark"
FAKE_POSTCODE = "SW1A 1AA"
FAKE_LATITUDE = 51.501
FAKE_LONGITUDE = -0.1419

# An Octopus account number
ACCOUNT_NUMBER_RE = re.compile(r"\bA-[0-9A-F]{8}\b")

# Field name in the Octopus account data -> prefix of its fake replacement
SENSITIVE_FIELDS = {"mpan": "BENCH-MPAN", "mprn": "BENCH-MPRN", "serialNumber": "BENCH-MSN", "deviceId": "BENCH-DEVICE"}


def collect_sensitive(account_cache):
    """Return {real value: fake value} for every identifier in an Octopus account cache entry.

    Every mpan, mprn and meter serialNumber gets a numbered fake, in order of appearance, and any Octopus account
    number (A- and eight hex digits) found in a key or value maps to FAKE_ACCOUNT. The keys under
    account_data.account are the agreement lists (electricityAgreements, gasAgreements), not identifiers.
    """
    replacements = {}
    counters = {prefix: 0 for prefix in SENSITIVE_FIELDS.values()}

    def note_accounts(text):
        """Record every account number inside text."""
        for number in ACCOUNT_NUMBER_RE.findall(text):
            replacements.setdefault(number, FAKE_ACCOUNT)

    def visit(obj):
        """Walk obj and record every sensitive field's value and account number."""
        if isinstance(obj, dict):
            for key, value in obj.items():
                if isinstance(key, str):
                    note_accounts(key)
                if key in SENSITIVE_FIELDS and isinstance(value, str) and value and value not in replacements:
                    prefix = SENSITIVE_FIELDS[key]
                    counters[prefix] += 1
                    replacements[value] = "{}-{}".format(prefix, counters[prefix])
                else:
                    visit(value)
        elif isinstance(obj, list):
            for item in obj:
                visit(item)
        elif isinstance(obj, str):
            note_accounts(obj)

    visit(account_cache or {})
    return replacements


def _replace_text(text, ordered):
    """Replace each real value in text with its fake, longest real values first."""
    for real, fake in ordered:
        if real in text:
            text = text.replace(real, fake)
    return text


def replace_everywhere(obj, replacements):
    """Return a copy of obj with every real value in replacements replaced in dict keys and string values.

    Real values are replaced as substrings, longest first, so a value that contains another is replaced whole.
    The input is never modified.
    """
    ordered = sorted(((real, fake) for real, fake in replacements.items() if real), key=lambda pair: len(pair[0]), reverse=True)

    def walk(value):
        """Rebuild value with the replacements applied."""
        if isinstance(value, str):
            return _replace_text(value, ordered)
        if isinstance(value, dict):
            return {walk(key): walk(item) for key, item in value.items()}
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, tuple):
            return tuple(walk(item) for item in value)
        return value

    return walk(obj)


def find_leaks(root, secrets: Iterable[str]):
    """Return the paths, relative to root and sorted, of files under root whose bytes contain any secret.

    Empty or None secrets are ignored, since they would match everything.
    """
    needles = [secret.encode() for secret in secrets if secret]
    root = Path(root)
    leaks = []
    for directory, _, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            data = path.read_bytes()
            if any(needle in data for needle in needles):
                leaks.append(path.relative_to(root).as_posix())
    return sorted(leaks)
