# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test the Hanchu command-line harness helpers
# -----------------------------------------------------------------------------

"""Tests for the pure helpers behind `python3 hanchu.py`, the live-hardware test harness.

The live drivers themselves talk to the real cloud and are excluded from coverage, so everything
that decides WHAT they write, what they restore and how they judge the result lives in these
helpers and is pinned here. A wrong answer from any of them is a wrong write to someone's inverter.
"""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import os
import tempfile
from hanchu import (
    HANCHU_CLI_RESTORE_KEYS,
    build_cli_schedule,
    cli_future_start,
    judge_live_window,
    load_cli_snapshot,
    merge_hanchu_credentials,
    restore_payload,
    save_cli_snapshot,
    settings_mismatches,
)
from hanchu_const import (
    HANCHU_KEY_CHARGE_POWER,
    HANCHU_KEY_CHARGE_SOC,
    HANCHU_KEY_DISCHARGE_POWER,
    HANCHU_KEY_DISCHARGE_SOC,
    HANCHU_KEY_GRID_CHARGE_SOC,
    HANCHU_KEY_WORK_MODE,
    HANCHU_RANGES,
)

SN = "HC240100001"
SETTINGS = {HANCHU_KEY_DISCHARGE_SOC: "10", HANCHU_KEY_WORK_MODE: "3"}


def expect(failed, condition, message):
    """Print message when condition is false, returning the accumulated failure flag."""
    if not condition:
        print(f"ERROR: {message}")
        return True
    return failed


def test_hanchu_cli_credentials_from_config():
    """apps.yaml fills what the command line left out, the command line wins, and a serial list gives its first entry."""
    failed = False
    config = {"hanchu_account": "me@example.com", "hanchu_password": "secret", "hanchu_inverter_sn": ["HC1", "HC2"]}
    merged, used = merge_hanchu_credentials({"account": None, "password": None, "serial": None}, config)
    failed = expect(failed, merged == {"account": "me@example.com", "password": "secret", "serial": "HC1"}, f"merged {merged}")
    failed = expect(failed, used == ["hanchu_account", "hanchu_password", "hanchu_inverter_sn"], f"used {used}")

    merged, used = merge_hanchu_credentials({"account": "other@example.com", "password": None, "serial": "HC9"}, config)
    failed = expect(failed, merged["account"] == "other@example.com" and merged["serial"] == "HC9", f"command line should win: {merged}")
    failed = expect(failed, used == ["hanchu_password"], f"only the password should come from the file: {used}")

    merged, used = merge_hanchu_credentials({"account": None, "password": None, "serial": None}, {"hanchu_inverter_sn": []})
    failed = expect(failed, merged["serial"] is None and used == [], f"an empty serial list supplies nothing: {merged} {used}")
    return failed


def test_hanchu_cli_charge_schedule_is_what_predbat_would_send():
    """A live charge test starts now, raises the target 10 points, and holds export at full rate as Predbat does outside an export."""
    failed = False
    schedule, notes = build_cli_schedule("charge", 600, 60, SETTINGS, HANCHU_RANGES, window_minutes=10, power=1500)
    charge = schedule["charge"]
    failed = expect(failed, charge == {"enable": True, "soc": 70, "power": 1500, "start": "10:00:00", "end": "10:10:00"}, f"charge {charge}")
    failed = expect(failed, not schedule["export"]["enable"], "export must be disabled")
    failed = expect(failed, schedule["export"]["power"] == HANCHU_RANGES[HANCHU_KEY_DISCHARGE_POWER][1], f"export power {schedule['export']['power']}")
    failed = expect(failed, schedule["reserve"] == 10, f"reserve should carry the device's current floor: {schedule['reserve']}")
    failed = expect(failed, notes == [], f"no notes expected: {notes}")
    return failed


def test_hanchu_cli_charge_target_respects_the_device_floor():
    """A charge target below the grid-charge limit's 20% floor is raised to it, and the note says so."""
    failed = False
    schedule, notes = build_cli_schedule("charge", 600, 5, SETTINGS, HANCHU_RANGES, window_minutes=10, power=1500)
    failed = expect(failed, schedule["charge"]["soc"] == 20, f"target {schedule['charge']['soc']}")
    failed = expect(failed, any("20" in note for note in notes), f"the raise should be explained: {notes}")
    # --target sets the charge target directly, so a tester can watch the battery stop at it.
    schedule, _ = build_cli_schedule("charge", 600, 60, SETTINGS, HANCHU_RANGES, window_minutes=10, power=1500, target=62)
    failed = expect(failed, schedule["charge"]["soc"] == 62, f"--target charge {schedule['charge']['soc']}")
    schedule, _ = build_cli_schedule("export", 600, 40, SETTINGS, HANCHU_RANGES, window_minutes=10, power=1500, target=38)
    failed = expect(failed, schedule["export"]["soc"] == 38, f"--target export floor {schedule['export']['soc']}")
    return failed


def test_hanchu_cli_power_is_capped_at_the_device_range():
    """A requested power above the range is capped, so the harness never asks for more than the device accepts."""
    failed = False
    ranges = dict(HANCHU_RANGES)
    ranges[HANCHU_KEY_CHARGE_POWER] = (0, 3680)
    schedule, notes = build_cli_schedule("charge", 600, 60, SETTINGS, ranges, window_minutes=10, power=9000)
    failed = expect(failed, schedule["charge"]["power"] == 3680, f"power {schedule['charge']['power']}")
    failed = expect(failed, any("3680" in note for note in notes), f"the cap should be explained: {notes}")
    return failed


def test_hanchu_cli_export_schedule_floors_inside_the_device_range():
    """An export test drops the floor 10 points but never above the device's 45% ceiling, and stops charging at full rate."""
    failed = False
    schedule, _ = build_cli_schedule("export", 600, 80, SETTINGS, HANCHU_RANGES, window_minutes=10, power=2000)
    export = schedule["export"]
    failed = expect(failed, export["enable"] and export["soc"] == 45 and export["power"] == 2000, f"export {export}")
    failed = expect(failed, schedule["charge"]["power"] == HANCHU_RANGES[HANCHU_KEY_CHARGE_POWER][1], f"charge power {schedule['charge']['power']}")

    schedule, _ = build_cli_schedule("export", 600, 30, SETTINGS, HANCHU_RANGES, window_minutes=10, power=2000)
    failed = expect(failed, schedule["export"]["soc"] == 20, f"floor should be SoC - 10: {schedule['export']['soc']}")
    return failed


def test_hanchu_cli_freeze_schedules_use_the_rates_alone():
    """Freeze charge holds through a zero discharge rate and freeze export through a zero charge rate, with no window - the shape execute.py produces."""
    failed = False
    schedule, _ = build_cli_schedule("freeze-charge", 600, 60, SETTINGS, HANCHU_RANGES, window_minutes=10, power=None)
    failed = expect(failed, not schedule["charge"]["enable"] and not schedule["export"]["enable"], "freeze charge writes no window")
    failed = expect(failed, schedule["export"]["power"] == 0 and schedule["charge"]["power"] == HANCHU_RANGES[HANCHU_KEY_CHARGE_POWER][1], f"freeze charge rates {schedule}")

    schedule, _ = build_cli_schedule("freeze-export", 600, 60, SETTINGS, HANCHU_RANGES, window_minutes=10, power=None)
    failed = expect(failed, not schedule["charge"]["enable"] and not schedule["export"]["enable"], "freeze export writes no window")
    failed = expect(failed, schedule["charge"]["power"] == 0 and schedule["export"]["power"] == HANCHU_RANGES[HANCHU_KEY_DISCHARGE_POWER][1], f"freeze export rates {schedule}")
    return failed


def test_hanchu_cli_refuses_a_test_that_could_show_nothing():
    """A full battery cannot show a charge, an empty one cannot show an export, and an unknown scenario is refused."""
    failed = False
    for scenario, soc in (("charge", 99), ("export", 6)):
        try:
            build_cli_schedule(scenario, 600, soc, SETTINGS, HANCHU_RANGES, window_minutes=10, power=1000)
            failed = expect(failed, False, f"{scenario} at {soc}% should be refused")
        except ValueError:
            pass
    try:
        build_cli_schedule("dance", 600, 50, SETTINGS, HANCHU_RANGES, window_minutes=10, power=1000)
        failed = expect(failed, False, "an unknown scenario should be refused")
    except ValueError:
        pass
    # A future write-test window does not need the battery to move, so a full battery is fine.
    schedule, _ = build_cli_schedule("charge", 600, 100, SETTINGS, HANCHU_RANGES, window_minutes=10, power=1000, require_effect=False)
    failed = expect(failed, schedule["charge"]["soc"] == 100, f"target {schedule['charge']['soc']}")
    return failed


def test_hanchu_cli_window_never_crosses_midnight():
    """A window running past midnight is cut at 23:59, and one with under two minutes left is refused."""
    failed = False
    schedule, notes = build_cli_schedule("charge", 23 * 60 + 52, 60, SETTINGS, HANCHU_RANGES, window_minutes=15, power=1000)
    failed = expect(failed, schedule["charge"]["end"] == "23:59:00", f"end {schedule['charge']['end']}")
    failed = expect(failed, any("midnight" in note for note in notes), f"the cut should be explained: {notes}")
    try:
        build_cli_schedule("charge", 23 * 60 + 58, 60, SETTINGS, HANCHU_RANGES, window_minutes=15, power=1000)
        failed = expect(failed, False, "a window with one minute left should be refused")
    except ValueError:
        pass
    return failed


def test_hanchu_cli_future_start_avoids_now_and_midnight():
    """The write test's window starts hours away from now and always fits before midnight."""
    failed = False
    for now in (0, 600, 1200, 1300, 1439):
        start = cli_future_start(now, 15)
        failed = expect(failed, 0 <= start and start + 15 <= 1439, f"now {now}: window {start}-{start + 15} must fit in the day")
        gap = min((start - now) % 1440, (now - (start + 15)) % 1440)
        failed = expect(failed, gap >= 60, f"now {now}: window at {start} is within an hour of now")
    return failed


def test_hanchu_cli_settings_compare_numerically():
    """Read-back values arrive as strings or floats; "3" and 3 match, a real difference or a missing key does not."""
    failed = False
    expected = {HANCHU_KEY_WORK_MODE: 3, HANCHU_KEY_CHARGE_SOC: 70, HANCHU_KEY_CHARGE_POWER: 1500}
    actual = {HANCHU_KEY_WORK_MODE: "3", HANCHU_KEY_CHARGE_SOC: 70.0, HANCHU_KEY_CHARGE_POWER: "1000"}
    mismatches = settings_mismatches(expected, actual)
    failed = expect(failed, mismatches == [(HANCHU_KEY_CHARGE_POWER, 1500, "1000")], f"mismatches {mismatches}")
    mismatches = settings_mismatches({HANCHU_KEY_CHARGE_SOC: 70}, {})
    failed = expect(failed, mismatches == [(HANCHU_KEY_CHARGE_SOC, 70, None)], f"a missing key is a mismatch: {mismatches}")
    return failed


def test_hanchu_cli_restore_payload_uses_the_snapshot():
    """Restore writes back exactly the keys asked for, as numbers, and names any the snapshot never held."""
    failed = False
    snapshot = {HANCHU_KEY_WORK_MODE: "1", HANCHU_KEY_CHARGE_SOC: "100", HANCHU_KEY_GRID_CHARGE_SOC: "80"}
    payload, missing = restore_payload(snapshot, [HANCHU_KEY_WORK_MODE, HANCHU_KEY_CHARGE_SOC, HANCHU_KEY_CHARGE_POWER])
    failed = expect(failed, payload == {HANCHU_KEY_WORK_MODE: 1, HANCHU_KEY_CHARGE_SOC: 100}, f"payload {payload}")
    failed = expect(failed, missing == [HANCHU_KEY_CHARGE_POWER], f"missing {missing}")
    # The overall charge cap is never written by Predbat, so a full restore leaves it alone too; the
    # grid-charge limit carries the charge target, so it is restored.
    failed = expect(failed, HANCHU_KEY_CHARGE_SOC not in HANCHU_CLI_RESTORE_KEYS, "the restore set should not include the overall charge cap")
    failed = expect(failed, HANCHU_KEY_GRID_CHARGE_SOC in HANCHU_CLI_RESTORE_KEYS, "the restore set should include the grid-charge limit")
    return failed


def test_hanchu_cli_snapshot_round_trips():
    """A snapshot saved before a write loads back with its serial and settings intact."""
    failed = False
    with tempfile.TemporaryDirectory() as folder:
        path = save_cli_snapshot(folder, SN, {HANCHU_KEY_WORK_MODE: "3"})
        failed = expect(failed, os.path.exists(path) and SN in os.path.basename(path), f"path {path}")
        serial, settings = load_cli_snapshot(path)
        failed = expect(failed, serial == SN and settings == {HANCHU_KEY_WORK_MODE: "3"}, f"loaded {serial} {settings}")
    return failed


def sample(elapsed, battery, load=500, pv=0, ac_pv=0, grid=0):
    """Build one live-window telemetry sample in Predbat's sign convention."""
    return {"elapsed": elapsed, "battery_power": battery, "load_power": load, "pv_power": pv, "ac_pv_power": ac_pv, "grid_power": grid, "soc": 50}


def test_hanchu_cli_judges_charge_and_export():
    """Charge passes when the battery charges (negative) after settling, export when it discharges; otherwise they fail."""
    failed = False
    verdict, _ = judge_live_window("charge", [sample(10, 0), sample(70, -1400), sample(100, -1500)])
    failed = expect(failed, verdict == "PASS", f"charge verdict {verdict}")
    verdict, reason = judge_live_window("charge", [sample(70, 300), sample(100, 280)])
    failed = expect(failed, verdict == "FAIL" and "did not charge" in reason, f"charge fail verdict {verdict}: {reason}")
    verdict, _ = judge_live_window("export", [sample(70, 2000), sample(100, 1900)])
    failed = expect(failed, verdict == "PASS", f"export verdict {verdict}")
    verdict, _ = judge_live_window("export", [sample(70, -500), sample(100, 0)])
    failed = expect(failed, verdict == "FAIL", f"export fail verdict {verdict}")
    # Only readings inside the settling time is not evidence either way.
    verdict, _ = judge_live_window("charge", [sample(10, -1500)])
    failed = expect(failed, verdict == "INCONCLUSIVE", f"settling-only verdict {verdict}")
    return failed


def test_hanchu_cli_judges_freezes_only_when_they_can_show_something():
    """A freeze is only judged while there is something to hold against: house demand for a charge freeze, a solar surplus for an export freeze."""
    failed = False
    # Freeze charge: demand exists, battery stays put -> PASS; battery feeds the house -> FAIL.
    verdict, _ = judge_live_window("freeze-charge", [sample(70, 0, load=800), sample(100, 20, load=800)])
    failed = expect(failed, verdict == "PASS", f"freeze-charge hold verdict {verdict}")
    verdict, _ = judge_live_window("freeze-charge", [sample(70, 750, load=800), sample(100, 760, load=800)])
    failed = expect(failed, verdict == "FAIL", f"freeze-charge drain verdict {verdict}")
    # Solar covering the load means a held battery and a free one look the same.
    verdict, _ = judge_live_window("freeze-charge", [sample(70, 0, load=300, pv=900), sample(100, 0, load=300, pv=900)])
    failed = expect(failed, verdict == "INCONCLUSIVE", f"freeze-charge no-demand verdict {verdict}")

    # Freeze export: surplus exists and the battery does not charge -> PASS; it soaks up the surplus -> FAIL.
    verdict, _ = judge_live_window("freeze-export", [sample(70, 0, load=300, pv=1500), sample(100, 0, load=300, pv=1500)])
    failed = expect(failed, verdict == "PASS", f"freeze-export verdict {verdict}")
    # pv_power already carries AC-coupled solar, so the bypass reading is not counted a second time.
    verdict, _ = judge_live_window("freeze-export", [sample(70, 0, load=1000, pv=600, ac_pv=600), sample(100, 0, load=1000, pv=600, ac_pv=600)])
    failed = expect(failed, verdict == "INCONCLUSIVE", f"AC solar counted twice: {verdict}")
    verdict, _ = judge_live_window("freeze-export", [sample(70, -1100, load=300, pv=1500), sample(100, -1200, load=300, pv=1500)])
    failed = expect(failed, verdict == "FAIL", f"freeze-export charging verdict {verdict}")
    verdict, _ = judge_live_window("freeze-export", [sample(70, 300, load=300), sample(100, 300, load=300)])
    failed = expect(failed, verdict == "INCONCLUSIVE", f"freeze-export no-surplus verdict {verdict}")
    return failed


def run_hanchu_cli_tests(my_predbat):
    """Run all Hanchu command-line harness helper tests."""
    failed = False
    for name, fn in [
        ("credentials", test_hanchu_cli_credentials_from_config),
        ("charge_schedule", test_hanchu_cli_charge_schedule_is_what_predbat_would_send),
        ("charge_floor", test_hanchu_cli_charge_target_respects_the_device_floor),
        ("power_cap", test_hanchu_cli_power_is_capped_at_the_device_range),
        ("export_schedule", test_hanchu_cli_export_schedule_floors_inside_the_device_range),
        ("freeze_schedules", test_hanchu_cli_freeze_schedules_use_the_rates_alone),
        ("refuse_no_effect", test_hanchu_cli_refuses_a_test_that_could_show_nothing),
        ("midnight", test_hanchu_cli_window_never_crosses_midnight),
        ("future_start", test_hanchu_cli_future_start_avoids_now_and_midnight),
        ("settings_compare", test_hanchu_cli_settings_compare_numerically),
        ("restore_payload", test_hanchu_cli_restore_payload_uses_the_snapshot),
        ("snapshot", test_hanchu_cli_snapshot_round_trips),
        ("judge_charge_export", test_hanchu_cli_judges_charge_and_export),
        ("judge_freezes", test_hanchu_cli_judges_freezes_only_when_they_can_show_something),
    ]:
        try:
            if fn():
                print(f"  FAILED: hanchu_cli.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in hanchu_cli.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
