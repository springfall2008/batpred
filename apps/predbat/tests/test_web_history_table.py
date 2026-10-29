# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
import asyncio
from datetime import datetime, timedelta, timezone

import pytz

from web import build_entity_history_table_data, render_entity_history_rows, WebInterface


def make_history(records):
    """Wrap a list of raw HA history records in the [[...]] shape returned by get_history_with_now."""
    return [records]


def utc(hour, minute, day=23, month=7):
    """Build a UTC timestamp on 2026-<month>-<day>."""
    return datetime(2026, month, day, hour, minute, 0, tzinfo=timezone.utc)


def record(when, state, attributes=None):
    """Build one HA history record stamped at the given time."""
    item = {"last_updated": when.strftime("%Y-%m-%dT%H:%M:%S%z"), "state": state}
    if attributes is not None:
        item["attributes"] = attributes
    return item


def find_row(rows, when):
    """Return the 30-minute row that starts at the given instant, or None."""
    for row in rows:
        if row["time"] == when:
            return row
    return None


def find_detail(row, when):
    """Return the 5-minute detail slot of a row at the given instant, or None."""
    for detail in row["details"]:
        if detail["time"] == when:
            return detail
    return None


def run_web_history_table_tests(my_predbat):
    """Unit tests for the /entity history table: build_entity_history_table_data() and its rendering."""
    failed = 0
    print("**** Running web history table tests ****")
    status = [{"entity_id": "sensor.status", "attribute": None}]

    # -------------------------------------------------------------------------
    # A row used to report the last sample taken inside its window, so a single momentary reading
    # stood for the whole half hour - the /entity table showed a GivEnergy Cloud inverter "Lost" for
    # hours off one blip. The value must be the state at the row's time, and the blip shows as blue.
    print("Test: a momentary blip does not become a row's value, and the row it falls in is marked changed-and-reverted")
    records = []
    stamp = utc(20, 0)
    while stamp < utc(23, 0):
        records.append(record(stamp, "Normal"))
        stamp += timedelta(minutes=5)
    records.append(record(utc(21, 57), "Lost"))
    rows = build_entity_history_table_data(status, {"sensor.status": make_history(records)})
    reported_lost = [row["time"].strftime("%H:%M") for row in rows if row["cells"][0][0] == "Lost"]
    if reported_lost:
        print(f"  ERROR: the sensor read Normal at every 30-min mark but these rows report 'Lost': {reported_lost}")
        failed += 1
    # 21:57 is first visible at the 22:00 slot, which is the 22:00 row's own line
    row = find_row(rows, utc(22, 0))
    if not row or row["cells"][0] != ("Normal", "reverted"):
        print(f"  ERROR: expected the 22:00 row to read Normal marked reverted, got {row['cells'] if row else None}")
        failed += 1
    for other in rows:
        if other["time"] != utc(22, 0) and other["cells"][0][1]:
            print(f"  ERROR: only the 22:00 row saw the blip, but {other['time'].strftime('%H:%M')} is marked {other['cells'][0][1]}")
            failed += 1

    # -------------------------------------------------------------------------
    print("Test: a change that sticks is pink in the row it happened in, and the expanded slot shows where")
    records = [record(utc(10, 0), "A"), record(utc(10, 10), "A"), record(utc(10, 40), "B"), record(utc(11, 50), "B")]
    rows = build_entity_history_table_data(status, {"sensor.status": make_history(records)})
    row_1030 = find_row(rows, utc(10, 30))
    if not row_1030 or row_1030["cells"][0] != ("A", "changed"):
        print(f"  ERROR: the 10:30 row should read A (its value at 10:30) and be marked changed, got {row_1030['cells'] if row_1030 else None}")
        failed += 1
    else:
        details = {detail["time"].strftime("%H:%M"): detail["cells"][0] for detail in row_1030["details"]}
        expected = {"10:55": ("B", None), "10:50": ("B", None), "10:45": ("B", None), "10:40": ("B", "changed"), "10:35": ("A", None)}
        if details != expected:
            print(f"  ERROR: expected the 10:30 row to expand to {expected}, got {details}")
            failed += 1
        detail_times = [detail["time"] for detail in row_1030["details"]]
        if detail_times != sorted(detail_times, reverse=True):
            print("  ERROR: detail slots should be newest first, matching the table's ordering")
            failed += 1
    row_1100 = find_row(rows, utc(11, 0))
    if not row_1100 or row_1100["cells"][0] != ("B", None):
        print(f"  ERROR: the change happened in the 10:30 row, so the 11:00 row should read B unmarked, got {row_1100['cells'] if row_1100 else None}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: a change that reverts inside a row is blue, and its slots show both changes")
    records = [record(utc(10, 0), "A"), record(utc(10, 37), "B"), record(utc(10, 48), "A"), record(utc(11, 20), "A")]
    rows = build_entity_history_table_data(status, {"sensor.status": make_history(records)})
    row_1030 = find_row(rows, utc(10, 30))
    if not row_1030 or row_1030["cells"][0] != ("A", "reverted"):
        print(f"  ERROR: expected the 10:30 row to read A marked reverted, got {row_1030['cells'] if row_1030 else None}")
        failed += 1
    else:
        changed_slots = {detail["time"].strftime("%H:%M"): detail["cells"][0] for detail in row_1030["details"] if detail["cells"][0][1]}
        if changed_slots != {"10:40": ("B", "changed"), "10:50": ("A", "changed")}:
            print(f"  ERROR: expected the 10:40 and 10:50 slots to show the change and the revert, got {changed_slots}")
            failed += 1

    # -------------------------------------------------------------------------
    print("Test: a change landing exactly on a row's time is reported by that row itself")
    records = [record(utc(10, 0), "A"), record(utc(10, 30), "B"), record(utc(11, 10), "B")]
    rows = build_entity_history_table_data(status, {"sensor.status": make_history(records)})
    if find_row(rows, utc(10, 30))["cells"][0] != ("B", "changed"):
        print(f"  ERROR: expected the 10:30 row to read B marked changed, got {find_row(rows, utc(10, 30))['cells'][0]}")
        failed += 1
    if find_row(rows, utc(10, 0))["cells"][0] != ("A", None):
        print(f"  ERROR: expected the 10:00 row to read A unmarked, got {find_row(rows, utc(10, 0))['cells'][0]}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: rows and slots never reach past the newest record")
    records = [record(utc(20, 0), "1"), record(utc(21, 12), "2")]
    rows = build_entity_history_table_data(status, {"sensor.status": make_history(records)})
    if rows[0]["time"] != utc(21, 0):
        print(f"  ERROR: the newest row should be 21:00, got {rows[0]['time']}")
        failed += 1
    future = [detail["time"].strftime("%H:%M") for row in rows for detail in row["details"] if detail["time"] > utc(21, 12)]
    if future:
        print(f"  ERROR: detail slots after the newest record (21:12) should not exist, got {future}")
        failed += 1
    if [detail["time"] for detail in rows[0]["details"]] != [utc(21, 10), utc(21, 5)]:
        print(f"  ERROR: expected the 21:00 row to expand to 21:10 and 21:05 only, got {[detail['time'].strftime('%H:%M') for detail in rows[0]['details']]}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: every 30-min row exists and reads the state at its own time")
    selections = [{"entity_id": "predbat.status", "attribute": None}]
    transitions = [
        (utc(22, 0, day=1, month=10), "Exporting"),
        (utc(23, 10, day=1, month=10), "Charging"),
        (utc(4, 35, day=2, month=10), "Demand"),
        (utc(13, 35, day=2, month=10), "Freeze exporting"),
    ]
    rows = build_entity_history_table_data(selections, {"predbat.status": make_history([record(when, state) for when, state in transitions])})

    def actual_state(when):
        """Return the state the entity really held at the given time."""
        held = "-"
        for changed_at, state in transitions:
            if changed_at <= when:
                held = state
        return held

    times = [row["time"] for row in rows]
    expected_times = [utc(13, 30, day=2, month=10) - timedelta(minutes=30 * index) for index in range(len(times))]
    if times != expected_times or times[-1] != utc(22, 0, day=1, month=10):
        print(f"  ERROR: expected a continuous run of rows from 13:30 back to 22:00, got {[when.strftime('%d %H:%M') for when in times]}")
        failed += 1
    wrong = [f"{row['time'].strftime('%d %H:%M')} reads {row['cells'][0][0]} but was {actual_state(row['time'])}" for row in rows if row["cells"][0][0] != actual_state(row["time"])]
    for row in rows:
        for detail in row["details"]:
            if detail["cells"][0][0] != actual_state(detail["time"]):
                wrong.append(f"{detail['time'].strftime('%d %H:%M')} reads {detail['cells'][0][0]} but was {actual_state(detail['time'])}")
    if wrong:
        print(f"  ERROR: {len(wrong)} cells do not match the time on their left, e.g. {wrong[0]}")
        failed += 1
    marked = {row["time"].strftime("%d %H:%M"): row["cells"][0][1] for row in rows if row["cells"][0][1]}
    if marked != {"01 23:00": "changed", "02 04:30": "changed", "02 13:30": "changed"}:
        print(f"  ERROR: expected exactly the rows each change happened in to be marked, got {marked}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: an entity's first value appearing is not a change")
    selections = [{"entity_id": "sensor.a", "attribute": None}, {"entity_id": "sensor.b", "attribute": None}]
    fetch = {
        "sensor.a": make_history([record(utc(10, 0), "A1"), record(utc(11, 0), "A1")]),
        "sensor.b": make_history([record(utc(10, 30), "B1")]),
    }
    rows = build_entity_history_table_data(selections, fetch)
    cells = {row["time"].strftime("%H:%M"): row["cells"] for row in rows}
    if cells.get("10:00") != [("A1", None), ("-", None)] or cells.get("10:30") != [("A1", None), ("B1", None)]:
        print(f"  ERROR: expected 10:00 [A1, -] and 10:30 [A1, B1] with nothing marked, got {cells}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: two columns for the same entity_id stay consistent with each other")
    selections = [{"entity_id": "sensor.x", "attribute": None}, {"entity_id": "sensor.x", "attribute": "other"}]
    records = [record(utc(10, minute), str(minute + 4), {"other": "o{}".format(minute)}) for minute in range(1, 5)]
    records.append(record(utc(10, 30), "8", {"other": "o4"}))
    rows = build_entity_history_table_data(selections, {"sensor.x": make_history(records)})
    detail = find_detail(find_row(rows, utc(10, 0)), utc(10, 5))
    if detail["cells"] != [("8", "changed"), ("o4", "changed")]:
        print(f"  ERROR: expected the 10:05 slot to read ['8', 'o4'] both changed, got {detail['cells']}")
        failed += 1

    # -------------------------------------------------------------------------
    # get_history_with_now() used to append the current state with no attributes, and a missing
    # attribute was shown as "None", so every attribute column read "None" from now on.
    print("Test: a record carrying no attributes at all says nothing about an attribute column")
    selections = [{"entity_id": "sensor.x", "attribute": "soc"}]
    records = [record(utc(10, 0), "on", {"soc": 50}), record(utc(10, 20), "on", {"soc": 60}), record(utc(10, 40), "on")]
    rows = build_entity_history_table_data(selections, {"sensor.x": make_history(records)})
    shown = [row["cells"][0][0] for row in rows] + [detail["cells"][0][0] for row in rows for detail in row["details"]]
    if "None" in shown:
        print(f"  ERROR: the attributes-less 10:40 record turned the soc column into 'None': {shown}")
        failed += 1
    row_1030 = find_row(rows, utc(10, 30))
    if not row_1030 or row_1030["cells"][0] != (60, None):
        print(f"  ERROR: expected the 10:30 row to hold soc 60 unmarked, got {row_1030['cells'] if row_1030 else None}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: records with a missing or unparseable last_updated are skipped and a missing state renders as 'None'")
    fetch = {"sensor.status": make_history([{"state": "no-timestamp"}, {"last_updated": "not-a-timestamp", "state": "unparseable"}, record(utc(12, 0), None)])}
    rows = build_entity_history_table_data(status, fetch)
    if len(rows) != 1 or rows[0]["cells"][0][0] != "None":
        print(f"  ERROR: expected one 12:00 row reading 'None', got {[(row['time'], row['cells']) for row in rows]}")
        failed += 1

    # -------------------------------------------------------------------------
    # HA/DB history is UTC; the table was shown in UTC too, an hour adrift of local time during BST.
    print("Test: rows are laid out and shown in the local timezone")
    london = pytz.timezone("Europe/London")
    records = [record(utc(20, 10), "A"), record(utc(20, 50), "B"), record(utc(21, 20), "B")]
    rows = build_entity_history_table_data(status, {"sensor.status": make_history(records)}, local_tz=london)
    labels = [row["time"].strftime("%H:%M%z") for row in rows]
    if labels != ["22:00+0100", "21:30+0100", "21:00+0100"]:
        print(f"  ERROR: expected rows 22:00, 21:30, 21:00 BST, got {labels}")
        failed += 1
    # Nepal is UTC+05:45, so its half hours do not fall on UTC half hours
    kathmandu = pytz.timezone("Asia/Kathmandu")
    rows = build_entity_history_table_data(status, {"sensor.status": make_history(records)}, local_tz=kathmandu)
    labels = [row["time"].strftime("%H:%M") for row in rows]
    if not labels or any(not label.endswith((":00", ":30")) for label in labels):
        print(f"  ERROR: rows should start on local half hours, got {labels}")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: rendering colours changed cells pink, reverted cells blue, and hides unmarked rows")
    records = [record(utc(8, 0), "A"), record(utc(10, 37), "B"), record(utc(10, 48), "A"), record(utc(12, 40), "C"), record(utc(13, 10), "C")]
    rows = build_entity_history_table_data(status, {"sensor.status": make_history(records)})
    html = render_entity_history_rows(rows, 1)
    if '<td class="reverted-cell">A</td>' not in html or '<td class="changed-cell">B</td>' not in html:
        print("  ERROR: expected the 10:30 row blue and its 10:40 slot pink")
        failed += 1
    shown_rows = html.count('class="history-row"')
    if shown_rows != 3:
        print(f"  ERROR: expected the 12:30 and 10:30 rows plus the oldest (08:00) to be shown, got {shown_rows}")
        failed += 1
    if "3 unchanged slots hidden" not in html.split("12:30", 1)[-1]:
        print("  ERROR: expected the 11:00-12:00 rows to be folded into '3 unchanged slots hidden' beneath the 12:30 row")
        failed += 1

    # -------------------------------------------------------------------------
    print("Test: get_history_with_now() stamps the current state at the real time and keeps its attributes")
    web = WebInterface(my_predbat, web_port=5053)
    web.get_history_wrapper = lambda entity_id, days, required=False, tracked=False: [[record(utc(10, 0), "on", {"soc": 50})]]
    web.get_state_wrapper = lambda entity_id=None, attribute=None, raw=False: {"state": "off", "attributes": {"soc": 70}} if raw else ("off" if attribute is None else 70)
    history = asyncio.run(web.get_history_with_now("sensor.x", 1))
    latest = history[0][-1]
    if latest.get("state") != "off" or latest.get("attributes", {}).get("soc") != 70:
        print(f"  ERROR: expected the appended record to carry state 'off' and soc 70, got {latest}")
        failed += 1
    elif abs((datetime.strptime(latest["last_updated"], "%Y-%m-%dT%H:%M:%S%z") - web.now_utc_exact).total_seconds()) > 60:
        print(f"  ERROR: expected the appended record stamped at the real time, got {latest['last_updated']}")
        failed += 1
    web.get_history_wrapper = lambda entity_id, days, required=False, tracked=False: []
    history = asyncio.run(web.get_history_with_now("sensor.x", 1, attribute="soc"))
    if not history or len(history[0]) != 2 or history[0][0].get("attributes", {}).get("soc") != 70:
        print(f"  ERROR: with no history an attribute should be seeded at the start of the window, got {history}")
        failed += 1

    return failed
