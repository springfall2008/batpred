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


def utc(hour, minute, day=23):
    """Build a UTC timestamp on 2026-07-<day>."""
    return datetime(2026, 7, day, hour, minute, 0, tzinfo=timezone.utc)


def run_web_history_table_tests(my_predbat):
    """Unit tests for build_entity_history_table_data() used by the /entity history table."""
    failed = 0
    print("**** Running web history table tests ****")

    # -------------------------------------------------------------------------
    # A row used to report the last sample taken inside its window, so a single momentary reading
    # stood for the whole half hour and then carried forward into every later slot without a sample
    # of its own - the /entity table showed a GivEnergy Cloud inverter "Lost" for hours off one blip.
    print("Test: a momentary sample inside a window does not become the whole row's value")
    selections = [{"entity_id": "sensor.status", "attribute": None}]
    records = []
    stamp = utc(20, 0, day=24)
    while stamp < utc(23, 0, day=24):
        records.append({"last_updated": stamp.strftime("%Y-%m-%dT%H:%M:%S%z"), "state": "Normal"})
        stamp += timedelta(minutes=5)
    records.append({"last_updated": utc(21, 57, day=24).strftime("%Y-%m-%dT%H:%M:%S%z"), "state": "Lost"})
    records.sort(key=lambda record: record["last_updated"])

    filled_30, filled_5, sorted_ts_30, display_slots_5 = build_entity_history_table_data(selections, {"sensor.status": make_history(records)})
    reported_lost = [ts for ts in sorted_ts_30 if filled_30[0][ts][0] == "Lost"]
    if reported_lost:
        print(f"  ERROR: the sensor read Normal at every 30-min mark but these rows report 'Lost': {[ts.strftime('%H:%M') for ts in reported_lost]}")
        failed += 1

    # -------------------------------------------------------------------------
    # Detail rows used to be the offsets -5..-25, i.e. the half hour BEFORE the row, so expanding a
    # row described a different window and could contradict the row it sat under.
    print("Test: detail slots belong to the row's own half hour, not the one before it")
    row_2130 = utc(21, 30, day=24)
    expected_slots = {ts + timedelta(minutes=offset) for ts in sorted_ts_30 for offset in range(0, 30, 5)}
    if display_slots_5 != expected_slots:
        print(f"  ERROR: detail slots should be each row's own half hour; {len(expected_slots - display_slots_5)} of them are missing and {len(display_slots_5 - expected_slots)} belong to another window")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a genuine transition is reported by the row it happened in, and by that row's detail slots")
    selections = [{"entity_id": "sensor.status", "attribute": None}]
    records = []
    stamp = utc(20, 0, day=24)
    while stamp < utc(23, 0, day=24):
        records.append({"last_updated": stamp.strftime("%Y-%m-%dT%H:%M:%S%z"), "state": "Normal" if stamp < row_2130 else "Lost"})
        stamp += timedelta(minutes=5)
    filled_30, filled_5, sorted_ts_30, display_slots_5 = build_entity_history_table_data(selections, {"sensor.status": make_history(records)})

    value, changed, prev_value = filled_30[0][row_2130]
    if value != "Lost" or not changed or prev_value != "Normal":
        print(f"  ERROR: expected the 21:30 row to flag the Normal->Lost change, got value={value} changed={changed} prev={prev_value}")
        failed += 1
    if filled_30[0][utc(21, 0, day=24)][0] != "Normal":
        print(f"  ERROR: expected the 21:00 row to still read 'Normal', got '{filled_30[0][utc(21, 0, day=24)][0]}'")
        failed += 1
    detail_values = {filled_5[0].get(row_2130 + timedelta(minutes=offset), ("-", False, None))[0] for offset in range(5, 30, 5)}
    if detail_values != {"Lost"}:
        print(f"  ERROR: expected the 21:30 row to expand to its own window (all 'Lost'), got {sorted(detail_values)}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a slot reports the state as of its own timestamp, not a reading taken later")
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

    value, changed, prev_value = filled_30[0][utc(10, 0)]
    if value != "-":
        print(f"  ERROR: nothing had been recorded by 10:00, so the row should read '-', got '{value}'")
        failed += 1
    if prev_value is not None:
        print(f"  ERROR: expected no prior value before the first row, got '{prev_value}'")
        failed += 1
    value, changed, _ = filled_5[0].get(utc(10, 5), ("-", False, None))
    if value != "8" or not changed:
        print(f"  ERROR: expected the 10:05 slot to report the latest reading '8' as a change, got value={value} changed={changed}")
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

    state_value, _, _ = filled_5[0].get(utc(10, 5), ("-", False, None))
    attr_value, _, _ = filled_5[1].get(utc(10, 5), ("-", False, None))
    if state_value != "8":
        print(f"  ERROR: state column should report the reading in effect at 10:05 ('8'), got '{state_value}'")
        failed += 1
    if attr_value != "o4":
        print(f"  ERROR: attribute column should report the value in effect at 10:05 ('o4'), got '{attr_value}'")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: 30-min row timestamps round down and never overflow the hour")
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

    # 10:30 is the row that first reports the 10:07 reading; 00:00 would be past the newest record
    expected_30 = {utc(10, 0), utc(10, 30), utc(23, 30)}
    if set(sorted_ts_30) != expected_30:
        print(f"  ERROR: expected 30-min rows {expected_30}, got {set(sorted_ts_30)}")
        failed += 1
    if sorted_ts_30 != sorted(sorted_ts_30, reverse=True):
        print("  ERROR: expected sorted_timestamps_30min in descending (newest-first) order")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a row with no sample of its own carries forward the previous known value")
    selections = [
        {"entity_id": "sensor.a", "attribute": None},
        {"entity_id": "sensor.b", "attribute": None},
    ]
    fetch = {
        "sensor.a": make_history([{"last_updated": "2026-07-23T10:00:00+00:00", "state": "A1"}]),
        "sensor.b": make_history([{"last_updated": "2026-07-23T10:30:00+00:00", "state": "B1"}]),
    }
    filled_30, filled_5, sorted_ts_30, display_slots_5 = build_entity_history_table_data(selections, fetch)

    value, changed, _ = filled_30[0][utc(10, 30)]
    if value != "A1" or changed:
        print(f"  ERROR: expected sensor.a to carry 'A1' forward unchanged at 10:30, got value={value} changed={changed}")
        failed += 1

    value, changed, prev_value = filled_30[1][utc(10, 0)]
    if value != "-" or changed or prev_value is not None:
        print(f"  ERROR: expected sensor.b to have no value before its first sample, got value={value} changed={changed} prev={prev_value}")
        failed += 1

    value, changed, _ = filled_30[1][utc(10, 30)]
    if value != "B1" or not changed:
        print(f"  ERROR: expected sensor.b to report its own sample 'B1' at 10:30 as a change, got value={value} changed={changed}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: records with a missing or unparseable last_updated are skipped and a missing state renders as 'None'")
    selections = [{"entity_id": "sensor.z", "attribute": None}]
    fetch = {
        "sensor.z": make_history(
            [
                {"state": "no-timestamp"},
                {"last_updated": "not-a-timestamp", "state": "unparseable"},
                {"last_updated": "2026-07-23T12:00:00+00:00", "state": None},
            ]
        )
    }
    filled_30, filled_5, sorted_ts_30, display_slots_5 = build_entity_history_table_data(selections, fetch)
    value, changed, _ = filled_30[0][utc(12, 0)]
    if value != "None" or not changed:
        print(f"  ERROR: expected a missing state to render as the string 'None', got value={value} changed={changed}")
        failed += 1
    if len(sorted_ts_30) != 1:
        print(f"  ERROR: expected the unusable records to be skipped, got rows {sorted_ts_30}")
        failed += 1

    # -------------------------------------------------------------------------
    # predbat.status only records on a change, so a window nothing was recorded in gets no row and
    # the page folds it into "N unchanged slots hidden". A change part way through a window (23:10)
    # is therefore never reported by a row: 23:00 still reads Exporting, 23:30 to 04:00 are hidden as
    # "unchanged" beneath it, and the 04:30 row is the first to say Charging - flagged as a change
    # five hours late, in the very window the charge ended. The timeline chart showed it correctly.
    print("Test: a change part way through a window is reported by the next row, not hidden as unchanged")
    selections = [{"entity_id": "predbat.status", "attribute": None}]

    def stamp(day, hour, minute):
        """Build a UTC timestamp on 2026-10-<day>."""
        return datetime(2026, 10, day, hour, minute, 0, tzinfo=timezone.utc)

    transitions = [
        (stamp(1, 22, 0), "Exporting"),
        (stamp(1, 23, 10), "Charging"),
        (stamp(2, 4, 35), "Demand"),
        (stamp(2, 13, 35), "Freeze exporting"),
    ]
    records = [{"last_updated": when.strftime("%Y-%m-%dT%H:%M:%S%z"), "state": state} for when, state in transitions]
    filled_30, filled_5, sorted_ts_30, display_slots_5 = build_entity_history_table_data(selections, {"predbat.status": make_history(records)})

    def actual_state(when):
        """Return the state the entity really held at the given time."""
        held = "-"
        for changed_at, state in transitions:
            if changed_at <= when:
                held = state
        return held

    row_2330 = stamp(1, 23, 30)
    if row_2330 not in sorted_ts_30:
        print(f"  ERROR: the status became Charging at 23:10 so the 23:30 row must be shown, rows are {[ts.strftime('%d %H:%M') for ts in sorted_ts_30]}")
        failed += 1
    else:
        value, changed, prev_value = filled_30[0][row_2330]
        if value != "Charging" or not changed or prev_value != "Exporting":
            print(f"  ERROR: expected the 23:30 row to flag the Exporting->Charging change, got value={value} changed={changed} prev={prev_value}")
            failed += 1

    value, changed, prev_value = filled_30[0][stamp(2, 4, 30)]
    if value != "Charging" or changed:
        print(f"  ERROR: Charging began at 23:10, so the 04:30 row should read 'Charging' without flagging a change, got value={value} changed={changed} prev={prev_value}")
        failed += 1

    # The page presents every 30-min slot missing between two rows as unchanged from the older row
    misreported = []
    for newer, older in zip(sorted_ts_30, sorted_ts_30[1:]):
        shown = filled_30[0][older][0]
        slot = older + timedelta(minutes=30)
        while slot < newer:
            if actual_state(slot) != shown:
                misreported.append("{} was {} but is hidden as unchanged from {}".format(slot.strftime("%d %H:%M"), actual_state(slot), shown))
            slot += timedelta(minutes=30)
    if misreported:
        print(f"  ERROR: {len(misreported)} hidden slots did not hold the value of the row beneath them, e.g. {misreported[0]}")
        failed += 1

    return failed
