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
SENSITIVE_FIELDS = {"mpan": "BENCH-MPAN", "mprn": "BENCH-MPRN", "serialNumber": "BENCH-MSN", "deviceId": "BENCH-DEVICE", "device_id": "BENCH-DEVICE"}


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


def with_case_variants(replacements, min_length=6):
    """Return replacements plus each real value lower-cased and with dashes as underscores, as integrations rewrite them.

    Values shorter than min_length are left as they are, since a short lower-cased value could match unrelated text.
    """
    result = dict(replacements)
    for real, fake in replacements.items():
        if not real or len(real) < min_length:
            continue
        for variant, variant_fake in ((real.lower(), fake.lower()), (real.replace("-", "_").lower(), fake.replace("-", "_").lower())):
            result.setdefault(variant, variant_fake)
    return result


def _files(root):
    """Yield (relative posix path, bytes) for every file under root."""
    root = Path(root)
    for directory, _, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            yield path.relative_to(root).as_posix(), path.read_bytes()


def find_leaks(root, secrets: Iterable[str], tokens: Iterable[str] = ()):
    """Return the paths, relative to root and sorted, of files under root that contain any secret, in any case.

    secrets match anywhere; tokens (device ids read off entity names) match only as a whole token, not inside a longer
    number or word. Empty or None values are ignored, since they would match everything.
    """
    needles = [secret.lower().encode() for secret in secrets if secret]
    bounded = [re.compile(_token_pattern(token).encode(), re.IGNORECASE) for token in tokens if token]
    leaks = []
    for path, data in _files(root):
        lowered = data.lower()
        if any(needle in lowered for needle in needles) or any(pattern.search(data) for pattern in bounded):
            leaks.append(path)
    return sorted(leaks)


def _token_pattern(token):
    """Return a regex matching token only where it is not part of a longer run of letters and digits."""
    return r"(?<![0-9A-Za-z])" + re.escape(token) + r"(?![0-9A-Za-z])"


def name_id_tokens(names):
    """Return {device id: fake} for the device ids inside entity names (Solax sites and serials, chargers, devices).

    A token of an entity name (split on "." and "_") is taken for an id when it has five or more digits, or is eight or
    more characters mixing letters with at least four digits (setting names such as pv_metric10_weight have fewer); the benchmark's own fakes (bench...) are not. Fakes are numbered in
    sorted order, so the same names always give the same fakes.
    """
    found = set()
    for name in names:
        for token in re.split(r"[._]", name):
            lowered = token.lower()
            if not token or lowered.startswith("bench"):
                continue
            digits = sum(char.isdigit() for char in token)
            if digits >= 5 or (len(token) >= 8 and digits >= 4 and any(char.isalpha() for char in token)):
                found.add(lowered)
    return {token: "benchid{}".format(number) for number, token in enumerate(sorted(found), start=1)}


def replace_tokens(text, tokens):
    """Replace each device id in tokens with its fake wherever it stands as a whole token, in any case."""
    for token, fake in sorted(tokens.items(), key=lambda pair: len(pair[0]), reverse=True):
        text = re.sub(_token_pattern(token), fake, text, flags=re.IGNORECASE)
    return text


# Attribute keys that hold a person's or a home's details, replaced at any depth (GE Cloud's account record, zones)
PERSONAL_FIELDS = {
    "first_name",
    "firstname",
    "surname",
    "last_name",
    "lastname",
    "full_name",
    "email",
    "address",
    "address1",
    "address2",
    "address3",
    "address_line_1",
    "address_line_2",
    "telephone_number",
    "phone",
    "phone_number",
    "mobile",
    "company",
    "company_name",
}


def scrub_personal_attributes(attributes):
    """Return a copy of attributes with coordinates set to the fake ones, postcodes to the fake and contact fields blanked."""

    def walk(value):
        """Rebuild value with personal fields replaced."""
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                name = key.lower() if isinstance(key, str) else key
                if name == "latitude" and isinstance(item, (int, float)):
                    result[key] = FAKE_LATITUDE
                elif name == "longitude" and isinstance(item, (int, float)):
                    result[key] = FAKE_LONGITUDE
                elif name in ("postcode", "post_code") and item:
                    result[key] = FAKE_POSTCODE
                elif name in PERSONAL_FIELDS and item:
                    result[key] = "benchmark"
                else:
                    result[key] = walk(item)
            return result
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(attributes)


EMAIL_RE = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE_RE = re.compile(rb"(?<![0-9])(?:\+44\s?7[0-9]{3}|07[0-9]{3})\s?[0-9]{3}\s?[0-9]{3}(?![0-9])")
POSTCODE_RE = re.compile(rb"(?<![A-Za-z0-9])([A-Z]{1,2}[0-9][A-Z0-9]?) ?([0-9][A-Z]{2})(?![A-Za-z0-9])")
COORDINATE_RE = re.compile(rb"[\"']?(latitude|longitude)[\"']?\s*[:=]\s*(-?[0-9]+(?:\.[0-9]+)?)")


def personal_data_kinds(data):
    """Return the kinds of personal data (email, phone, postcode, coordinates) that bytes look like they hold."""
    kinds = set()
    if EMAIL_RE.search(data):
        kinds.add("email")
    if PHONE_RE.search(data):
        kinds.add("phone")
    for match in POSTCODE_RE.finditer(data):
        if match.group(1) + b" " + match.group(2) != FAKE_POSTCODE.encode():
            kinds.add("postcode")
            break
    for match in COORDINATE_RE.finditer(data):
        fake = FAKE_LATITUDE if match.group(1) == b"latitude" else FAKE_LONGITUDE
        if float(match.group(2)) != fake:
            kinds.add("coordinates")
            break
    return kinds


# Public data that is not personal but has fields shaped like it (vehicle and charger models such as "P90DL")
PUBLIC_FILES = ("octopus_vehicle_catalogue",)


def find_personal_data(root, public=PUBLIC_FILES):
    """Return sorted (path, kind) for the files under root that look like they hold personal data, never the values.

    Files whose name starts with one of public (Octopus's vehicle catalogue) are public data and are not scanned.
    """
    return sorted((path, kind) for path, data in _files(root) if not Path(path).name.startswith(tuple(public)) for kind in personal_data_kinds(data))
