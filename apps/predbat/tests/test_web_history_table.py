# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from datetime import datetime, timedelta, timezone

from web import build_entity_history_table_data


def make_history(records):
    """Wrap a list of raw HA history records in the [[...]] shape returned by get_history_with_now."""
    return [records]


def run_web_history_table_tests(my_predbat):
    """Unit tests for build_entity_history_table_data() used by the /entity history table."""
    failed = 0
    print("**** Running web history table tests ****")

    # -------------------------------------------------------------------------
    print("Test: 30-min bucket keeps the most recent sample, not the oldest, when several samples land in the same window")
    selections = [{"entity_id": "sensor.x", "attribute": None}]
    fetch = {
        "sensor.x": make_history(
            [
                {"last_updated": "2026-07-23T10:01:00+00:00", "state": "5"},
                {"last_updated": "2026-07-23T10:02:00+00:00", "state": "6"},
                {"last_updated": "2026-07-23T10:03:00+00:00", "state": "7"},
                {"last_updated": "2026-07-23T10:04:00+00:00", "state": "8"},
            ]
        )
    }
    filled_30, filled_5, sorted_ts_30, display_slots_5 = build_entity_history_table_data(selections, fetch)

    bucket_30 = datetime(2026, 7, 23, 10, 0, 0, tzinfo=timezone.utc)
    value, is_known, prev_value = filled_30[0][bucket_30]
    if value != "8":
        print(f"  ERROR: expected 30-min bucket to hold the latest sample '8', got '{value}'")
        failed += 1
    if not is_known:
        print("  ERROR: expected 30-min bucket to be flagged as directly known")
        failed += 1
    if prev_value is not None:
        print(f"  ERROR: expected no prior value before the first bucket, got '{prev_value}'")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: 5-min bucket keeps the most recent sample, not the oldest, when several samples land in the same window")
    selections = [{"entity_id": "sensor.x", "attribute": None}]
    fetch = {
        "sensor.x": make_history(
            [
                {"last_updated": "2026-07-23T10:26:00+00:00", "state": "5"},
                {"last_updated": "2026-07-23T10:27:00+00:00", "state": "6"},
                {"last_updated": "2026-07-23T10:28:00+00:00", "state": "7"},
                {"last_updated": "2026-07-23T10:29:00+00:00", "state": "8"},
                # A later sample in the next 30-min window, purely so 10:25 becomes a rendered detail slot
                # (detail slots are the offsets -5..-25 leading up to each known 30-min bucket).
                {"last_updated": "2026-07-23T10:35:00+00:00", "state": "9"},
            ]
        )
    }
    filled_30, filled_5, sorted_ts_30, display_slots_5 = build_entity_history_table_data(selections, fetch)

    bucket_5 = datetime(2026, 7, 23, 10, 25, 0, tzinfo=timezone.utc)
    if bucket_5 not in display_slots_5:
        print(f"  ERROR: test setup expected {bucket_5} to be a rendered detail slot, got {display_slots_5}")
        failed += 1
    value, is_known, prev_value = filled_5[0][bucket_5]
    if value != "8":
        print(f"  ERROR: expected 5-min bucket to hold the latest sample '8', got '{value}'")
        failed += 1
    if not is_known:
        print("  ERROR: expected 5-min bucket to be flagged as directly known")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: two columns for the same entity_id stay consistent with each other")
    selections = [
        {"entity_id": "sensor.x", "attribute": None},
        {"entity_id": "sensor.x", "attribute": "other"},
    ]
    fetch = {
        "sensor.x": make_history(
            [
                {"last_updated": "2026-07-23T10:01:00+00:00", "state": "5", "attributes": {"other": "o1"}},
                {"last_updated": "2026-07-23T10:02:00+00:00", "state": "6", "attributes": {"other": "o2"}},
                {"last_updated": "2026-07-23T10:03:00+00:00", "state": "7", "attributes": {"other": "o3"}},
                {"last_updated": "2026-07-23T10:04:00+00:00", "state": "8", "attributes": {"other": "o4"}},
            ]
        )
    }
    filled_30, filled_5, sorted_ts_30, display_slots_5 = build_entity_history_table_data(selections, fetch)

    state_value, _, _ = filled_30[0][bucket_30]
    attr_value, _, _ = filled_30[1][bucket_30]
    if state_value != "8":
        print(f"  ERROR: state column should pick the latest sample '8', got '{state_value}'")
        failed += 1
    if attr_value != "o4":
        print(f"  ERROR: attribute column should pick the latest sample 'o4', got '{attr_value}'")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: 30-min bucket timestamps round down and never overflow the hour")
    selections = [{"entity_id": "sensor.y", "attribute": None}]
    fetch = {
        "sensor.y": make_history(
            [
                {"last_updated": "2026-07-23T10:07:00+00:00", "state": "mid"},
                {"last_updated": "2026-07-23T23:59:00+00:00", "state": "late"},
            ]
        )
    }
    filled_30, filled_5, sorted_ts_30, display_slots_5 = build_entity_history_table_data(selections, fetch)

    expected_30 = {
        datetime(2026, 7, 23, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 23, 23, 30, 0, tzinfo=timezone.utc),
    }
    if set(sorted_ts_30) != expected_30:
        print(f"  ERROR: expected 30-min buckets {expected_30}, got {set(sorted_ts_30)}")
        failed += 1
    if sorted_ts_30 != sorted(sorted_ts_30, reverse=True):
        print("  ERROR: expected sorted_timestamps_30min in descending (newest-first) order")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: display slots for detail rows are the 5-min offsets leading up to each 30-min bucket")
    selections = [{"entity_id": "sensor.x", "attribute": None}]
    fetch = {
        "sensor.x": make_history(
            [
                {"last_updated": "2026-07-23T10:01:00+00:00", "state": "5"},
                {"last_updated": "2026-07-23T10:35:00+00:00", "state": "9"},
            ]
        )
    }
    filled_30, filled_5, sorted_ts_30, display_slots_5 = build_entity_history_table_data(selections, fetch)

    # Expected: the 5-min offsets (-5 to -25) leading up to each of the two known 30-min buckets (10:00 and 10:30)
    expected_slots = set()
    for base_hour, base_minute in ((10, 0), (10, 30)):
        base = datetime(2026, 7, 23, base_hour, base_minute, 0, tzinfo=timezone.utc)
        for offset in (5, 10, 15, 20, 25):
            expected_slots.add(base - timedelta(minutes=offset))
    if display_slots_5 != expected_slots:
        print(f"  ERROR: expected display slots {expected_slots}, got {display_slots_5}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a 30-min bucket with no direct sample carries forward the previous known value")
    selections = [
        {"entity_id": "sensor.a", "attribute": None},
        {"entity_id": "sensor.b", "attribute": None},
    ]
    fetch = {
        "sensor.a": make_history([{"last_updated": "2026-07-23T10:00:00+00:00", "state": "A1"}]),
        "sensor.b": make_history([{"last_updated": "2026-07-23T10:30:00+00:00", "state": "B1"}]),
    }
    filled_30, filled_5, sorted_ts_30, display_slots_5 = build_entity_history_table_data(selections, fetch)

    ts_1000 = datetime(2026, 7, 23, 10, 0, 0, tzinfo=timezone.utc)
    ts_1030 = datetime(2026, 7, 23, 10, 30, 0, tzinfo=timezone.utc)

    value, is_known, prev_value = filled_30[0][ts_1030]
    if value != "A1" or is_known:
        print(f"  ERROR: expected sensor.a to carry forward 'A1' as unknown at 10:30, got value={value} is_known={is_known}")
        failed += 1

    value, is_known, prev_value = filled_30[1][ts_1000]
    if value != "-" or is_known or prev_value is not None:
        print(f"  ERROR: expected sensor.b to have no value before its first sample at 10:00, got value={value} is_known={is_known} prev_value={prev_value}")
        failed += 1

    value, is_known, prev_value = filled_30[1][ts_1030]
    if value != "B1" or not is_known:
        print(f"  ERROR: expected sensor.b to show its own sample 'B1' at 10:30, got value={value} is_known={is_known}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: records missing last_updated are skipped and a missing state renders as 'None'")
    selections = [{"entity_id": "sensor.z", "attribute": None}]
    fetch = {
        "sensor.z": make_history(
            [
                {"state": "no-timestamp"},
                {"last_updated": "2026-07-23T12:00:00+00:00", "state": None},
            ]
        )
    }
    filled_30, filled_5, sorted_ts_30, display_slots_5 = build_entity_history_table_data(selections, fetch)
    ts_1200 = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)
    value, is_known, _ = filled_30[0][ts_1200]
    if value != "None" or not is_known:
        print(f"  ERROR: expected a missing state to render as the string 'None', got value={value} is_known={is_known}")
        failed += 1
    if len(sorted_ts_30) != 1:
        print(f"  ERROR: expected the record without last_updated to be skipped, got buckets {sorted_ts_30}")
        failed += 1

    return failed
