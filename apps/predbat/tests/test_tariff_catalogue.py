# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the tariff catalogue used by the Annual tab's dropdown."""

from tariff_catalogue import BUILTIN_TARIFFS, CUSTOM_ID, convert_compare_entry, merged_catalogue


def test_tariff_catalogue(my_predbat):
    """Verify the built-in catalogue, the Compare key mapping, and the merge."""
    failed = False
    print("**** Testing tariff_catalogue ****")

    print("Test: every built-in entry has an id, a name and at least an import URL")
    if not BUILTIN_TARIFFS:
        print("  ERROR: the built-in catalogue is empty")
        failed = True
    seen_ids = set()
    for entry in BUILTIN_TARIFFS:
        for key in ["id", "name"]:
            if not entry.get(key):
                print("  ERROR: entry {} is missing '{}'".format(entry, key))
                failed = True
        if not entry.get("import_octopus_url") and not entry.get("rates_import"):
            print("  ERROR: entry {} has neither an import URL nor fixed rates".format(entry.get("id")))
            failed = True
        if entry.get("id") in seen_ids:
            print("  ERROR: duplicate id {}".format(entry.get("id")))
            failed = True
        seen_ids.add(entry.get("id"))

    print("Test: the built-in catalogue contains exactly the expected ids")
    expected_ids = [
        "cap_seg",
        "eon_next_drive",
        "igo_fixed",
        "igo_prime",
        "igo_agile",
        "go_fixed",
        "go_prime",
        "go_agile",
        "agile_fixed",
        "agile_prime",
        "agile_agile",
        "flux",
        "cosy_fixed",
        "cosy_prime",
        "cosy_agile",
        "snug_fixed",
        "snug_prime",
        "iflux",
    ]
    actual_ids = [entry["id"] for entry in BUILTIN_TARIFFS]
    if actual_ids != expected_ids:
        print("  ERROR: built-in ids changed, expected {} got {}".format(expected_ids, actual_ids))
        failed = True

    print("Test: no built-in entry uses Compare's key names")
    for entry in BUILTIN_TARIFFS:
        for stale in ["rates_import_octopus_url", "rates_export_octopus_url"]:
            if stale in entry:
                print("  ERROR: entry {} still uses Compare's key '{}'".format(entry.get("id"), stale))
                failed = True

    print("Test: convert_compare_entry maps Compare's URL keys onto the engine's")
    converted = convert_compare_entry(
        {
            "id": "agile_agile",
            "name": "Agile import/Agile export",
            "rates_import_octopus_url": "https://example.com/import/",
            "rates_export_octopus_url": "https://example.com/export/",
        }
    )
    if converted is None:
        print("  ERROR: a valid Compare entry should convert")
        failed = True
    else:
        if converted.get("import_octopus_url") != "https://example.com/import/":
            print("  ERROR: import URL not mapped, got {}".format(converted))
            failed = True
        if converted.get("export_octopus_url") != "https://example.com/export/":
            print("  ERROR: export URL not mapped, got {}".format(converted))
            failed = True
        if "rates_import_octopus_url" in converted:
            print("  ERROR: the Compare key should not survive conversion")
            failed = True

    print("Test: fixed rate structures pass through unchanged")
    converted = convert_compare_entry({"id": "cap", "name": "Price cap", "rates_import": [{"rate": 24.86}], "rates_export": [{"rate": 4.1}]})
    if converted is None or converted.get("rates_import") != [{"rate": 24.86}]:
        print("  ERROR: fixed rates should pass through, got {}".format(converted))
        failed = True

    print("Test: an entry with no usable rate source is rejected rather than shown")
    if convert_compare_entry({"id": "current", "name": "Current Tariff"}) is not None:
        print("  ERROR: an entry with no rates should be rejected")
        failed = True
    if convert_compare_entry({"name": "No id"}) is not None:
        print("  ERROR: an entry with no id should be rejected")
        failed = True
    if convert_compare_entry("not a dict") is not None:
        print("  ERROR: a non-dict should be rejected rather than raising")
        failed = True

    print("Test: merged_catalogue with no user list returns the built-ins plus Custom")
    merged = merged_catalogue(None)
    if len(merged) != len(BUILTIN_TARIFFS) + 1:
        print("  ERROR: expected {} entries, got {}".format(len(BUILTIN_TARIFFS) + 1, len(merged)))
        failed = True
    if merged[-1]["id"] != CUSTOM_ID:
        print("  ERROR: Custom should be the last entry, got {}".format(merged[-1]))
        failed = True

    print("Test: a user's compare_list is merged in and does not clobber a built-in id")
    builtin_id = BUILTIN_TARIFFS[0]["id"]
    merged = merged_catalogue(
        [
            {"id": builtin_id, "name": "My override", "rates_import_octopus_url": "https://example.com/mine/"},
            {"id": "my_tariff", "name": "My tariff", "rates_import": [{"rate": 20.0}]},
        ]
    )
    ids = [entry["id"] for entry in merged]
    if ids.count(builtin_id) != 1:
        print("  ERROR: a user entry sharing a built-in id should not duplicate it, ids were {}".format(ids))
        failed = True
    if "my_tariff" not in ids:
        print("  ERROR: a new user entry should appear, ids were {}".format(ids))
        failed = True

    print("Test: a malformed user entry is skipped rather than breaking the dropdown")
    merged = merged_catalogue([{"junk": True}, None, "string", {"id": "ok", "name": "Ok", "rates_import": [{"rate": 5.0}]}])
    ids = [entry["id"] for entry in merged]
    if "ok" not in ids:
        print("  ERROR: the valid entry should survive alongside malformed ones, ids were {}".format(ids))
        failed = True

    return failed
