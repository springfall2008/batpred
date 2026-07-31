# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the tariff catalogue used by the Annual tab's dropdowns."""

from tariff_catalogue import (
    BASELINE_DEFAULT_EXPORT_ID,
    BASELINE_DEFAULT_IMPORT_ID,
    CUSTOM_ID,
    EXPORT_TARIFFS,
    IMPORT_TARIFFS,
    NO_EXPORT_ID,
    convert_compare_entry,
    match_entry,
    merged_export_catalogue,
    merged_import_catalogue,
)


def test_tariff_catalogue(my_predbat):
    """Verify the built-in catalogues, the Compare key mapping, and the two merges."""
    failed = False
    print("**** Testing tariff_catalogue ****")

    print("Test: every built-in entry has an id, a name and a rate source for its own side")
    for name, builtins, url_key, rates_key in [
        ("import", IMPORT_TARIFFS, "import_octopus_url", "rates_import"),
        ("export", EXPORT_TARIFFS, "export_octopus_url", "rates_export"),
    ]:
        if not builtins:
            print("  ERROR: the built-in {} catalogue is empty".format(name))
            failed = True
        seen_ids = set()
        for entry in builtins:
            for key in ["id", "name"]:
                if not entry.get(key):
                    print("  ERROR: {} entry {} is missing '{}'".format(name, entry, key))
                    failed = True
            if entry.get(url_key) is None and entry.get(rates_key) is None:
                print("  ERROR: {} entry {} has neither a URL nor fixed rates".format(name, entry.get("id")))
                failed = True
            if entry.get("id") in seen_ids:
                print("  ERROR: duplicate {} id {}".format(name, entry.get("id")))
                failed = True
            seen_ids.add(entry.get("id"))

    print("Test: an import entry carries no export keys, and vice versa")
    # The whole point of the split is that a side is chosen independently. An entry
    # carrying the other side's keys would silently reintroduce a fixed pairing.
    for entry in IMPORT_TARIFFS:
        for stray in ["export_octopus_url", "rates_export"]:
            if stray in entry:
                print("  ERROR: import entry {} carries export key '{}'".format(entry.get("id"), stray))
                failed = True
    for entry in EXPORT_TARIFFS:
        for stray in ["import_octopus_url", "rates_import"]:
            if stray in entry:
                print("  ERROR: export entry {} carries import key '{}'".format(entry.get("id"), stray))
                failed = True

    print("Test: the built-in catalogues contain exactly the expected ids")
    expected_import = ["price_cap", "eon_next_drive", "agile", "intelligent_go", "go", "cosy", "snug", "flux", "intelligent_flux", "edf_go", "edf_empower_fixed"]
    actual_import = [entry["id"] for entry in IMPORT_TARIFFS]
    if actual_import != expected_import:
        print("  ERROR: import ids changed, expected {} got {}".format(expected_import, actual_import))
        failed = True
    expected_export = [NO_EXPORT_ID, "seg", "outgoing_fixed", "outgoing_prime", "agile_outgoing", "flux_export", "intelligent_flux_export", "eon_next_export", "edf_export", "edf_export_exclusive"]
    actual_export = [entry["id"] for entry in EXPORT_TARIFFS]
    if actual_export != expected_export:
        print("  ERROR: export ids changed, expected {} got {}".format(expected_export, actual_export))
        failed = True

    print("Test: the baseline defaults name entries that actually exist")
    if BASELINE_DEFAULT_IMPORT_ID not in actual_import:
        print("  ERROR: baseline import default {} is not in the import catalogue".format(BASELINE_DEFAULT_IMPORT_ID))
        failed = True
    if BASELINE_DEFAULT_EXPORT_ID not in actual_export:
        print("  ERROR: baseline export default {} is not in the export catalogue".format(BASELINE_DEFAULT_EXPORT_ID))
        failed = True

    print("Test: 'no export' is a flat zero rate, not an absent tariff")
    # An absent export leaves every minute unstamped, which reads as "unknown" rather
    # than "unpaid"; the whole option is only meaningful as an explicit zero.
    no_export = [entry for entry in EXPORT_TARIFFS if entry["id"] == NO_EXPORT_ID][0]
    if no_export.get("rates_export") != [{"rate": 0.0}]:
        print("  ERROR: no-export entry should be a flat 0p rate, got {}".format(no_export))
        failed = True

    print("Test: no built-in entry uses Compare's key names")
    for entry in IMPORT_TARIFFS + EXPORT_TARIFFS:
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

    print("Test: each merge with no user list returns its built-ins plus Custom")
    for name, merged, builtins in [
        ("import", merged_import_catalogue(None), IMPORT_TARIFFS),
        ("export", merged_export_catalogue(None), EXPORT_TARIFFS),
    ]:
        if len(merged) != len(builtins) + 1:
            print("  ERROR: {} expected {} entries, got {}".format(name, len(builtins) + 1, len(merged)))
            failed = True
        if merged[-1]["id"] != CUSTOM_ID:
            print("  ERROR: {} Custom should be the last entry, got {}".format(name, merged[-1]))
            failed = True

    print("Test: one paired compare_list entry is offered in BOTH dropdowns")
    # A user's compare_list entry is a pair, but the dropdowns are not - so the entry
    # has to be decomposed rather than dropped from one side or forced into both.
    paired = [{"id": "mine", "name": "My deal", "rates_import_octopus_url": "https://example.com/i/", "rates_export_octopus_url": "https://example.com/e/"}]
    import_side = [entry for entry in merged_import_catalogue(paired) if entry["id"] == "mine"]
    export_side = [entry for entry in merged_export_catalogue(paired) if entry["id"] == "mine"]
    if not import_side or import_side[0].get("import_octopus_url") != "https://example.com/i/":
        print("  ERROR: the paired entry should appear in the import list, got {}".format(import_side))
        failed = True
    if not export_side or export_side[0].get("export_octopus_url") != "https://example.com/e/":
        print("  ERROR: the paired entry should appear in the export list, got {}".format(export_side))
        failed = True
    if import_side and ("export_octopus_url" in import_side[0] or "rates_export" in import_side[0]):
        print("  ERROR: the import half should not carry export keys, got {}".format(import_side[0]))
        failed = True

    print("Test: an import-only compare_list entry is not offered as an export tariff")
    # Offering it would produce a run priced against an export tariff that does not
    # exist, which reads downstream as "export earns nothing" with no warning.
    import_only = [{"id": "import_only", "name": "Import only", "rates_import": [{"rate": 9.0}]}]
    if "import_only" not in [entry["id"] for entry in merged_import_catalogue(import_only)]:
        print("  ERROR: an import-only entry should appear in the import list")
        failed = True
    if "import_only" in [entry["id"] for entry in merged_export_catalogue(import_only)]:
        print("  ERROR: an import-only entry must not appear in the export list")
        failed = True

    print("Test: a user entry sharing a built-in id replaces it rather than duplicating it")
    for name, merge, builtin_id in [
        ("import", merged_import_catalogue, IMPORT_TARIFFS[0]["id"]),
        ("export", merged_export_catalogue, EXPORT_TARIFFS[0]["id"]),
    ]:
        merged = merge(
            [
                {"id": builtin_id, "name": "My override", "rates_import_octopus_url": "https://example.com/mine/", "rates_export_octopus_url": "https://example.com/mine-e/"},
                {"id": "my_tariff", "name": "My tariff", "rates_import": [{"rate": 20.0}], "rates_export": [{"rate": 3.0}]},
            ]
        )
        ids = [entry["id"] for entry in merged]
        if ids.count(builtin_id) != 1:
            print("  ERROR: {} a user entry sharing a built-in id should not duplicate it, ids were {}".format(name, ids))
            failed = True
        if "my_tariff" not in ids:
            print("  ERROR: {} a new user entry should appear, ids were {}".format(name, ids))
            failed = True

    print("Test: a malformed user entry is skipped rather than breaking the dropdowns")
    for name, merge in [("import", merged_import_catalogue), ("export", merged_export_catalogue)]:
        merged = merge([{"junk": True}, None, "string", {"id": "ok", "name": "Ok", "rates_import": [{"rate": 5.0}], "rates_export": [{"rate": 5.0}]}])
        if "ok" not in [entry["id"] for entry in merged]:
            print("  ERROR: {} the valid entry should survive alongside malformed ones".format(name))
            failed = True

    print("Test: match_entry finds the entry a tariff side was chosen from, by URL and by rates")
    imports = merged_import_catalogue()
    exports = merged_export_catalogue()
    agile = [entry for entry in IMPORT_TARIFFS if entry["id"] == "agile"][0]
    matched = match_entry(imports, {"import_octopus_url": agile["import_octopus_url"]}, "import_octopus_url", "rates_import")
    if not matched or matched.get("id") != "agile":
        print("  ERROR: a URL side should match its catalogue entry, got {}".format(matched))
        failed = True
    price_cap = [entry for entry in IMPORT_TARIFFS if entry["id"] == BASELINE_DEFAULT_IMPORT_ID][0]
    matched = match_entry(imports, {"rates_import": price_cap["rates_import"]}, "import_octopus_url", "rates_import")
    if not matched or matched.get("id") != BASELINE_DEFAULT_IMPORT_ID:
        print("  ERROR: a basic-rates side should match on its rates, got {}".format(matched))
        failed = True
    no_export = [entry for entry in EXPORT_TARIFFS if entry["id"] == NO_EXPORT_ID][0]
    matched = match_entry(exports, {"rates_export": no_export["rates_export"]}, "export_octopus_url", "rates_export")
    if not matched or matched.get("id") != NO_EXPORT_ID:
        print("  ERROR: a deliberate no-export side should match its own entry, got {}".format(matched))
        failed = True

    print("Test: match_entry returns None for a side that matches nothing, and for one that is not set")
    # None rather than the Custom entry: Custom carries no rate source, so returning it
    # would let a caller read a name off an entry that describes no actual tariff.
    for tariff, label in [
        ({"import_octopus_url": "https://example.com/mine/"}, "a hand-entered URL"),
        ({"rates_import": [{"rate": 99.5}]}, "a rate that is in no entry"),
        ({}, "an unset side"),
        (None, "no tariff at all"),
    ]:
        matched = match_entry(imports, tariff, "import_octopus_url", "rates_import")
        if matched is not None:
            print("  ERROR: {} should match nothing, got {}".format(label, matched))
            failed = True

    print("Test: match_entry matches each side against its own list, so the two cannot be confused")
    # An export URL handed to the import side must not match: the pairing bug this
    # catalogue replaced came from matching one side's value against the wrong list.
    outgoing = [entry for entry in EXPORT_TARIFFS if entry.get("export_octopus_url")][0]
    if match_entry(imports, {"import_octopus_url": outgoing["export_octopus_url"]}, "import_octopus_url", "rates_import") is not None:
        print("  ERROR: an export URL should not match an import catalogue entry")
        failed = True

    print("Test: match_entry sees the user's own compare_list entries, not just the built-ins")
    # This is why naming happens where the merged catalogue is available: a user tariff
    # would otherwise fall back to a bare product code despite having a name.
    user_list = [{"id": "mine", "name": "My deal", "rates_import": [{"rate": 12.34}]}]
    matched = match_entry(merged_import_catalogue(user_list), {"rates_import": [{"rate": 12.34}]}, "import_octopus_url", "rates_import")
    if not matched or matched.get("name") != "My deal":
        print("  ERROR: a compare_list entry should be matchable by name, got {}".format(matched))
        failed = True

    print("Test: the Custom placeholder is never returned even when a tariff carries nothing")
    if any(match_entry(catalogue, {}, url_key, rates_key) for catalogue, url_key, rates_key in [(imports, "import_octopus_url", "rates_import"), (exports, "export_octopus_url", "rates_export")]):
        print("  ERROR: an empty tariff should match nothing at all")
        failed = True

    return failed
