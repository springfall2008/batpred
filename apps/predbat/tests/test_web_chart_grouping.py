# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from web import split_entities_for_charting


def make_history(records):
    """Wrap a list of raw HA history records in the [[...]] shape returned by get_history_with_now."""
    return [records]


def run_web_chart_grouping_tests(my_predbat):
    """Unit tests for split_entities_for_charting() - a unit group can mix numeric and non-numeric entities."""
    failed = 0
    print("**** Running web chart grouping tests ****")

    # -------------------------------------------------------------------------
    print("Test: a numeric entity is charted even when a non-numeric entity in the same group is processed after it")
    entities = [
        {"id": "number.percent", "friendly_name": "Percent", "attribute": None},
        {"id": "select.status", "friendly_name": "Status", "attribute": None},
    ]
    entity_data_fetch = {
        "number.percent": make_history([{"last_updated": "2026-07-23T10:00:00+00:00", "state": "45"}, {"last_updated": "2026-07-23T10:05:00+00:00", "state": "50"}]),
        "select.status": make_history([{"last_updated": "2026-07-23T10:00:00+00:00", "state": "Demand"}, {"last_updated": "2026-07-23T10:05:00+00:00", "state": "Idle"}]),
    }
    numeric_entries, timeline_entries = split_entities_for_charting(entities, entity_data_fetch)

    numeric_ids = [e["entity_id"] for e in numeric_entries]
    timeline_ids = [e["entity_id"] for e in timeline_entries]
    if "number.percent" not in numeric_ids:
        print(f"  ERROR: expected the numeric entity to be charted as a line series, got numeric entries: {numeric_ids}")
        failed += 1
    if "select.status" not in timeline_ids:
        print(f"  ERROR: expected the non-numeric entity to be charted as a timeline series, got timeline entries: {timeline_ids}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: the same split holds regardless of entity order (numeric entity listed first)")
    entities_reordered = [
        {"id": "select.status", "friendly_name": "Status", "attribute": None},
        {"id": "number.percent", "friendly_name": "Percent", "attribute": None},
    ]
    numeric_entries, timeline_entries = split_entities_for_charting(entities_reordered, entity_data_fetch)
    numeric_ids = [e["entity_id"] for e in numeric_entries]
    timeline_ids = [e["entity_id"] for e in timeline_entries]
    if "number.percent" not in numeric_ids:
        print(f"  ERROR: order shouldn't matter - expected the numeric entity charted as a line series, got numeric entries: {numeric_ids}")
        failed += 1
    if "select.status" not in timeline_ids:
        print(f"  ERROR: order shouldn't matter - expected the non-numeric entity charted as a timeline series, got timeline entries: {timeline_ids}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: an entity with no history data is skipped entirely")
    entities_missing = [{"id": "number.missing", "friendly_name": "Missing", "attribute": None}]
    numeric_entries, timeline_entries = split_entities_for_charting(entities_missing, {"number.missing": make_history([])})
    if numeric_entries or timeline_entries:
        print(f"  ERROR: expected no entries for an entity with no history, got numeric={numeric_entries} timeline={timeline_entries}")
        failed += 1

    return failed
