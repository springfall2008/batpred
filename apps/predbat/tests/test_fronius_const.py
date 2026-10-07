# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Fronius constants and pure schedule helpers
# -----------------------------------------------------------------------------

"""Tests for fronius_const.py: time conversion (incl. DST), segments, commands and value helpers."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import pytz
from datetime import date, datetime, timedelta, timezone
from fronius_const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ACTION_HOLD,
    ACTION_IDLE,
    FRONIUS_PARAMS_LIST,
    FRONIUS_PARAMS_OBJECT,
    ceil_minute,
    channel_values,
    clip_segments,
    daily_energy_kwh,
    describe_error,
    hms_to_minutes,
    horizon_end,
    local_wall_to_utc,
    make_segment,
    merge_segments,
    parse_error_body,
    parse_zulu,
    predbat_battery_power,
    predbat_grid_power,
    predbat_load_power,
    schedule_hash,
    segments_equivalent,
    segments_from_json,
    segments_to_commands,
    segments_to_json,
    short_system_id,
    window_to_utc,
    zulu,
)

LONDON = pytz.timezone("Europe/London")
VIENNA = pytz.timezone("Europe/Vienna")


def utc(*args):
    """Build an aware UTC datetime."""
    return datetime(*args, tzinfo=timezone.utc)


def test_fronius_zulu_formatting():
    """dispatchDateTime is UTC with a Z suffix, converted from any aware time."""
    assert zulu(utc(2026, 6, 15, 12, 30)) == "2026-06-15T12:30:00Z"
    assert zulu(LONDON.localize(datetime(2026, 6, 15, 13, 30))) == "2026-06-15T12:30:00Z"
    assert zulu(VIENNA.localize(datetime(2026, 1, 15, 13, 30))) == "2026-01-15T12:30:00Z"


def test_fronius_parse_zulu():
    """Timestamps parse with Z, an offset, or the seven fractional digits the rate-limit header uses."""
    assert parse_zulu("2026-03-28T13:00:00.0000000Z") == utc(2026, 3, 28, 13, 0)
    assert parse_zulu("2026-03-28T13:00:00Z") == utc(2026, 3, 28, 13, 0)
    assert parse_zulu("2026-03-28T14:00:00+01:00") == utc(2026, 3, 28, 13, 0)
    assert parse_zulu("2026-03-28T08:00:00-05:00") == utc(2026, 3, 28, 13, 0)
    assert parse_zulu("") is None and parse_zulu(None) is None and parse_zulu("garbage") is None


def test_fronius_hms_to_minutes():
    """Predbat's HH:MM[:SS] strings become minutes; junk becomes None."""
    assert hms_to_minutes("02:30:00") == 150
    assert hms_to_minutes("23:59") == 1439
    assert hms_to_minutes("24:00:00") == 1440
    assert hms_to_minutes("") is None and hms_to_minutes("unknown") is None and hms_to_minutes("10:75:00") is None


def test_fronius_local_wall_to_utc_regular_days():
    """Winter and summer offsets are applied for both zones."""
    assert local_wall_to_utc(date(2026, 1, 15), 120, LONDON) == utc(2026, 1, 15, 2, 0)
    assert local_wall_to_utc(date(2026, 6, 15), 120, LONDON) == utc(2026, 6, 15, 1, 0)
    assert local_wall_to_utc(date(2026, 1, 15), 120, VIENNA) == utc(2026, 1, 15, 1, 0)
    assert local_wall_to_utc(date(2026, 6, 15), 120, VIENNA) == utc(2026, 6, 15, 0, 0)
    # Minutes past a day roll onto the next date on the wall clock.
    assert local_wall_to_utc(date(2026, 6, 15), 24 * 60 + 30, LONDON) == utc(2026, 6, 15, 23, 30)


def test_fronius_local_wall_to_utc_spring_forward():
    """A wall time inside the spring-forward gap lands just after the gap."""
    # London 2026-03-29: 01:00 GMT -> 02:00 BST, so 01:30 does not exist.
    assert local_wall_to_utc(date(2026, 3, 29), 90, LONDON) == utc(2026, 3, 29, 1, 30)
    assert local_wall_to_utc(date(2026, 3, 29), 60 * 3, LONDON) == utc(2026, 3, 29, 2, 0)
    # Vienna 2026-03-29: 02:00 CET -> 03:00 CEST, so 02:30 does not exist.
    assert local_wall_to_utc(date(2026, 3, 29), 150, VIENNA) == utc(2026, 3, 29, 1, 30)
    assert local_wall_to_utc(date(2026, 3, 29), 60 * 4, VIENNA) == utc(2026, 3, 29, 2, 0)


def test_fronius_local_wall_to_utc_fall_back():
    """A wall time in the repeated autumn hour is read as its first occurrence."""
    # London 2026-10-25: 02:00 BST -> 01:00 GMT, so 01:30 happens twice.
    assert local_wall_to_utc(date(2026, 10, 25), 90, LONDON) == utc(2026, 10, 25, 0, 30)
    assert local_wall_to_utc(date(2026, 10, 25), 60 * 3, LONDON) == utc(2026, 10, 25, 3, 0)
    # Vienna 2026-10-25: 03:00 CEST -> 02:00 CET, so 02:30 happens twice.
    assert local_wall_to_utc(date(2026, 10, 25), 150, VIENNA) == utc(2026, 10, 25, 0, 30)
    assert local_wall_to_utc(date(2026, 10, 25), 60 * 4, VIENNA) == utc(2026, 10, 25, 3, 0)


def test_fronius_local_wall_to_utc_zoneinfo():
    """A zoneinfo zone resolves the same DST edge cases as pytz."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return
    london = ZoneInfo("Europe/London")
    assert local_wall_to_utc(date(2026, 6, 15), 120, london) == utc(2026, 6, 15, 1, 0)
    assert local_wall_to_utc(date(2026, 3, 29), 90, london) == utc(2026, 3, 29, 1, 30)
    assert local_wall_to_utc(date(2026, 10, 25), 90, london) == utc(2026, 10, 25, 0, 30)


def test_fronius_window_to_utc_follows_compute_window_minutes():
    """Future, already-passed, midnight-spanning and empty windows map like inverter.py reads them."""
    now = utc(2026, 6, 15, 12, 0)  # 13:00 BST
    assert window_to_utc("14:00:00", "15:30:00", now, LONDON) == (utc(2026, 6, 15, 13, 0), utc(2026, 6, 15, 14, 30))
    # Already started: today's instance, starting in the past.
    assert window_to_utc("12:30:00", "14:00:00", now, LONDON) == (utc(2026, 6, 15, 11, 30), utc(2026, 6, 15, 13, 0))
    # Already ended today: tomorrow's.
    assert window_to_utc("02:00:00", "05:00:00", now, LONDON) == (utc(2026, 6, 16, 1, 0), utc(2026, 6, 16, 4, 0))
    # Spans midnight.
    assert window_to_utc("23:00:00", "01:00:00", now, LONDON) == (utc(2026, 6, 15, 22, 0), utc(2026, 6, 16, 0, 0))
    late = utc(2026, 6, 15, 23, 30)  # 00:30 BST on the 16th
    assert window_to_utc("23:00:00", "01:00:00", late, LONDON) == (utc(2026, 6, 15, 22, 0), utc(2026, 6, 16, 0, 0))
    assert window_to_utc("00:00:00", "00:00:00", now, LONDON) is None
    assert window_to_utc("bad", "01:00:00", now, LONDON) is None


def test_fronius_window_to_utc_on_dst_days():
    """A window across a DST change keeps its wall-clock ends, so its real length changes."""
    # London spring forward: 00:30-04:30 local is 00:30Z-03:30Z, three real hours.
    start, end = window_to_utc("00:30:00", "04:30:00", utc(2026, 3, 29, 0, 0), LONDON)
    assert (start, end) == (utc(2026, 3, 29, 0, 30), utc(2026, 3, 29, 3, 30)), (start, end)
    # London fall back: 00:30-04:30 local is 23:30Z-04:30Z, five real hours.
    start, end = window_to_utc("00:30:00", "04:30:00", utc(2026, 10, 24, 23, 0), LONDON)
    assert (start, end) == (utc(2026, 10, 24, 23, 30), utc(2026, 10, 25, 4, 30)), (start, end)
    # Vienna spring forward: 01:00-05:00 local is 00:00Z-03:00Z.
    start, end = window_to_utc("01:00:00", "05:00:00", utc(2026, 3, 28, 23, 30), VIENNA)
    assert (start, end) == (utc(2026, 3, 29, 0, 0), utc(2026, 3, 29, 3, 0)), (start, end)
    # Vienna fall back: 01:00-05:00 local is 23:00Z-04:00Z.
    start, end = window_to_utc("01:00:00", "05:00:00", utc(2026, 10, 24, 22, 30), VIENNA)
    assert (start, end) == (utc(2026, 10, 24, 23, 0), utc(2026, 10, 25, 4, 0)), (start, end)


def test_fronius_ceil_minute_and_horizon():
    """The start rounds up to a whole minute; the horizon to the next 30-minute boundary past +60."""
    assert ceil_minute(utc(2026, 6, 15, 12, 0, 0)) == utc(2026, 6, 15, 12, 0)
    assert ceil_minute(utc(2026, 6, 15, 12, 0, 1)) == utc(2026, 6, 15, 12, 1)
    assert horizon_end(utc(2026, 6, 15, 12, 0)) == utc(2026, 6, 15, 13, 0)
    assert horizon_end(utc(2026, 6, 15, 12, 1)) == utc(2026, 6, 15, 13, 30)
    assert horizon_end(utc(2026, 6, 15, 12, 29)) == utc(2026, 6, 15, 13, 30)
    assert horizon_end(utc(2026, 6, 15, 12, 30)) == utc(2026, 6, 15, 13, 30)


def test_fronius_merge_and_clip():
    """Adjacent identical segments merge; clipping drops idle and trims to the range."""
    a = make_segment(utc(2026, 6, 15, 12, 0), utc(2026, 6, 15, 12, 30), ACTION_CHARGE, {"MinW": 3000, "MaxW": 3000})
    b = make_segment(utc(2026, 6, 15, 12, 30), utc(2026, 6, 15, 13, 0), ACTION_CHARGE, {"MinW": 3000, "MaxW": 3000})
    c = make_segment(utc(2026, 6, 15, 13, 0), utc(2026, 6, 15, 13, 30), ACTION_IDLE, {})
    merged = merge_segments([c, b, a])
    assert len(merged) == 2 and merged[0]["end"] == utc(2026, 6, 15, 13, 0), merged
    clipped = clip_segments(merged, utc(2026, 6, 15, 12, 10), utc(2026, 6, 15, 12, 40))
    assert clipped == [make_segment(utc(2026, 6, 15, 12, 10), utc(2026, 6, 15, 12, 40), ACTION_CHARGE, {"MinW": 3000, "MaxW": 3000})], clipped


def test_fronius_equivalence_ignores_the_moving_start():
    """Two schedules that do the same thing from now on are equivalent, whatever their start times."""
    old = [make_segment(utc(2026, 6, 15, 11, 50), utc(2026, 6, 15, 13, 0), ACTION_CHARGE, {"MinW": 3000, "MaxW": 3000})]
    new = [make_segment(utc(2026, 6, 15, 12, 0), utc(2026, 6, 15, 13, 30), ACTION_CHARGE, {"MinW": 3000, "MaxW": 3000})]
    start = utc(2026, 6, 15, 12, 0)
    assert segments_equivalent(old, new, start, start + timedelta(minutes=60))
    assert not segments_equivalent(old, new, start, start + timedelta(minutes=61)), "a horizon past the old end must differ"
    other = [make_segment(utc(2026, 6, 15, 12, 0), utc(2026, 6, 15, 13, 30), ACTION_HOLD, {"MinW": 0, "MaxW": 0})]
    assert not segments_equivalent(old, other, start, start + timedelta(minutes=60))
    assert segments_equivalent([], [], start, start + timedelta(minutes=60))


def test_fronius_segments_to_commands():
    """Commands carry type, Zulu start, integer duration, a payload id and the chosen parameter shape."""
    segments = [
        make_segment(utc(2026, 6, 15, 12, 0), utc(2026, 6, 15, 12, 30), ACTION_CHARGE, {"MinW": 3000, "MaxW": 3000}),
        make_segment(utc(2026, 6, 15, 12, 30), utc(2026, 6, 15, 12, 30, 30), ACTION_HOLD, {"MinW": 0, "MaxW": 0}),
        make_segment(utc(2026, 6, 15, 12, 31), utc(2026, 6, 15, 13, 0), ACTION_IDLE, {}),
        make_segment(utc(2026, 6, 15, 13, 0), utc(2026, 6, 15, 13, 30), ACTION_DISCHARGE, {"MinW": 2500, "MaxW": 2500}),
    ]
    commands = segments_to_commands(segments, "predbat-abc", FRONIUS_PARAMS_LIST)
    assert len(commands) == 2, commands
    first, second = commands
    assert first == {
        "dispatchType": "ChargeBattery",
        "dispatchDateTime": "2026-06-15T12:00:00Z",
        "dispatchDuration": 1800,
        "dispatchParameters": [{"name": "MaxW", "value": 3000}, {"name": "MinW", "value": 3000}],
        "dispatchPayload": "predbat-abc-1",
    }, first
    assert second["dispatchType"] == "DischargeBattery" and second["dispatchPayload"] == "predbat-abc-2"
    assert isinstance(second["dispatchDuration"], int)
    as_object = segments_to_commands(segments, "predbat-abc", FRONIUS_PARAMS_OBJECT)
    assert as_object[0]["dispatchParameters"] == {"MaxW": 3000, "MinW": 3000}, as_object[0]


def test_fronius_segments_json_round_trip_and_hash():
    """Segments survive storage; the hash is stable and ignores idle segments."""
    segments = [
        make_segment(utc(2026, 6, 15, 12, 0), utc(2026, 6, 15, 12, 30), ACTION_CHARGE, {"MinW": 3000, "MaxW": 3000}),
        make_segment(utc(2026, 6, 15, 12, 30), utc(2026, 6, 15, 13, 0), ACTION_IDLE, {}),
    ]
    restored = segments_from_json(segments_to_json(segments))
    assert restored == segments, restored
    assert schedule_hash(segments) == schedule_hash(segments[:1])
    assert schedule_hash(segments) != schedule_hash([make_segment(utc(2026, 6, 15, 12, 0), utc(2026, 6, 15, 12, 30), ACTION_HOLD, {"MinW": 0, "MaxW": 0})])
    assert segments_from_json([{"start": "bad"}, None]) == []


def test_fronius_sign_and_value_helpers():
    """Grid is negated, battery passes through, load is made positive, NULLs are dropped."""
    assert predbat_grid_power(-1500) == 1500 and predbat_grid_power(800) == -800 and predbat_grid_power(None) is None
    assert predbat_battery_power(-1200) == -1200 and predbat_battery_power(900) == 900
    assert predbat_load_power(-800) == 800 and predbat_load_power(None) is None
    values = channel_values([{"channelName": "BattSOC", "value": 50}, {"channelName": "PowerPV", "value": None}, {"channelName": "PowerLoad", "value": "x"}, "junk"])
    assert values == {"BattSOC": 50.0}, values


def test_fronius_daily_energy_kwh():
    """Import/export include the battery grid legs; a missing primary channel leaves the leaf out."""
    values = {"EnergyPurchased": 4000, "EnergyBattChargeGrid": 1000, "EnergyFeedIn": 6000, "EnergyConsumptionTotal": 9000}
    assert daily_energy_kwh(values) == {"import_today": 5.0, "export_today": 6.0, "load_today": 9.0}
    assert daily_energy_kwh({}) == {}


def test_fronius_error_helpers():
    """Error bodies parse to (int code, message), including a string code; descriptions add a gloss."""
    assert parse_error_body({"responseError": "10406", "responseMessage": "PV System has no battery."}) == (10406, "PV System has no battery.")
    assert parse_error_body("not a dict") == (None, "")
    assert describe_error(10406, "PV System has no battery.") == "10406 (PV system has no battery): PV System has no battery."
    assert describe_error(99999) == "99999"
    assert describe_error(None, "boom") == "(no code): boom"


def test_fronius_short_system_id():
    """The entity-name id is the first eight hex digits of the GUID."""
    assert short_system_id("A6582E07-80B1-4313-89F9-9F98FDB0A289") == "a6582e07"
    assert short_system_id("") == "system"


def run_fronius_const_tests(my_predbat):
    """Run all Fronius constants/helper tests."""
    failed = False
    for name, fn in [
        ("zulu", test_fronius_zulu_formatting),
        ("parse_zulu", test_fronius_parse_zulu),
        ("hms_to_minutes", test_fronius_hms_to_minutes),
        ("wall_to_utc_regular", test_fronius_local_wall_to_utc_regular_days),
        ("wall_to_utc_spring", test_fronius_local_wall_to_utc_spring_forward),
        ("wall_to_utc_fall", test_fronius_local_wall_to_utc_fall_back),
        ("wall_to_utc_zoneinfo", test_fronius_local_wall_to_utc_zoneinfo),
        ("window_to_utc", test_fronius_window_to_utc_follows_compute_window_minutes),
        ("window_to_utc_dst", test_fronius_window_to_utc_on_dst_days),
        ("ceil_and_horizon", test_fronius_ceil_minute_and_horizon),
        ("merge_and_clip", test_fronius_merge_and_clip),
        ("equivalence", test_fronius_equivalence_ignores_the_moving_start),
        ("commands", test_fronius_segments_to_commands),
        ("json_and_hash", test_fronius_segments_json_round_trip_and_hash),
        ("sign_helpers", test_fronius_sign_and_value_helpers),
        ("daily_energy", test_fronius_daily_energy_kwh),
        ("error_helpers", test_fronius_error_helpers),
        ("short_id", test_fronius_short_system_id),
    ]:
        try:
            if fn():
                print(f"  FAILED: fronius_const.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in fronius_const.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
