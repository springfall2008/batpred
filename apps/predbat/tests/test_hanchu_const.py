# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Hanchu ESS cloud constants
# -----------------------------------------------------------------------------

"""Tests for the Hanchu constants module (``hanchu_const.py``)."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
from hanchu_const import (
    HANCHU_BASE_URL,
    HANCHU_CONTROL_KEYS,
    HANCHU_ENDPOINTS,
    HANCHU_ENERGY,
    HANCHU_KEY_CHARGE_END,
    HANCHU_KEY_CHARGE_POWER,
    HANCHU_KEY_CHARGE_SOC,
    HANCHU_KEY_CHARGE_START,
    HANCHU_KEY_DISCHARGE_END,
    HANCHU_KEY_DISCHARGE_POWER,
    HANCHU_KEY_DISCHARGE_SOC,
    HANCHU_KEY_DISCHARGE_START,
    HANCHU_KEY_WORK_MODE,
    HANCHU_RANGES,
    HANCHU_SECONDS_PER_DAY,
    HANCHU_SLOT_COUNT,
    HANCHU_SLOT_DISABLED,
    HANCHU_TELEMETRY,
    HANCHU_TELEMETRY_NEGATE,
    HANCHU_WORK_MODE_FOR_SCHEDULE,
    HANCHU_WORK_MODES,
    clamp_range,
    hhmmss_to_seconds,
    scale_to_watts,
    seconds_to_hhmmss,
    split_window_seconds,
    window_is_empty,
)


def test_hanchu_base_url_and_endpoints():
    """The base URL and every endpoint path match what the reference integrations post to."""
    failed = False
    if HANCHU_BASE_URL != "https://iess3.hanchuess.com":
        print(f"ERROR: base URL {HANCHU_BASE_URL}")
        failed = True
    expected = {
        "login": "/gateway/identify/auth/token",
        "refresh": "/gateway/identify/auth/token/refresh",
        "device_list": "/gateway/app/ha/getDeviceList",
        "device_status": "/gateway/app/ha/getDeviceStatus",
        "device_statistics": "/gateway/app/ha/getDeviceStatistics",
        "menu": "/gateway/app/ha/menu",
        "iot_get": "/gateway/app/ha/iotGet",
        "iot_set": "/gateway/app/ha/iotSet",
        "fast_charge": "/gateway/app/ha/fastChargeDischarge",
    }
    for key, path in expected.items():
        if HANCHU_ENDPOINTS.get(key) != path:
            print(f"ERROR: endpoint {key} = {HANCHU_ENDPOINTS.get(key)} != {path}")
            failed = True
    extra = set(HANCHU_ENDPOINTS) - set(expected)
    if extra:
        # An endpoint added without a provenance annotation is exactly the failure mode the
        # provenance rules exist to prevent, so a new one has to be added here deliberately.
        print(f"ERROR: undocumented extra endpoints {sorted(extra)}")
        failed = True
    assert not failed, "test_hanchu_base_url_and_endpoints"


def test_hanchu_control_keys_cover_every_setting_predbat_writes():
    """The one batched iotGet asks for every key the write path can send, and nothing is missing."""
    failed = False
    for key in (HANCHU_KEY_WORK_MODE, HANCHU_KEY_CHARGE_POWER, HANCHU_KEY_DISCHARGE_POWER, HANCHU_KEY_CHARGE_SOC, HANCHU_KEY_DISCHARGE_SOC):
        if key not in HANCHU_CONTROL_KEYS:
            print(f"ERROR: control key {key} missing from the read set")
            failed = True
    # Three charge slots and three discharge slots, each with a start and an end: twelve keys.
    for template in (HANCHU_KEY_CHARGE_START, HANCHU_KEY_CHARGE_END, HANCHU_KEY_DISCHARGE_START, HANCHU_KEY_DISCHARGE_END):
        for index in range(1, HANCHU_SLOT_COUNT + 1):
            if template.format(index) not in HANCHU_CONTROL_KEYS:
                print(f"ERROR: slot key {template.format(index)} missing from the read set")
                failed = True
    if len(HANCHU_CONTROL_KEYS) != len(set(HANCHU_CONTROL_KEYS)):
        print("ERROR: the read set contains duplicates")
        failed = True
    # Six scalars plus twelve slot keys. A count check as well as the membership checks, so an
    # accidental extra key cannot slip in unnoticed.
    if len(HANCHU_CONTROL_KEYS) != 18:
        print(f"ERROR: expected 18 control keys, got {len(HANCHU_CONTROL_KEYS)}")
        failed = True
    assert not failed, "test_hanchu_control_keys_cover_every_setting_predbat_writes"


def test_hanchu_ranges_encode_the_narrow_soc_limits():
    """The two SoC limits are narrower than Predbat's own 0-100, which the component must clamp to."""
    failed = False
    if HANCHU_RANGES.get(HANCHU_KEY_CHARGE_SOC) != (50, 100):
        print(f"ERROR: charge SoC range {HANCHU_RANGES.get(HANCHU_KEY_CHARGE_SOC)} != (50, 100)")
        failed = True
    if HANCHU_RANGES.get(HANCHU_KEY_DISCHARGE_SOC) != (5, 45):
        print(f"ERROR: discharge SoC range {HANCHU_RANGES.get(HANCHU_KEY_DISCHARGE_SOC)} != (5, 45)")
        failed = True
    # Zero must be INSIDE both power ranges: zero is how Predbat expresses freeze, so a range
    # starting at 1 would silently turn every freeze into a 1 W trickle.
    for key in (HANCHU_KEY_CHARGE_POWER, HANCHU_KEY_DISCHARGE_POWER):
        low, _high = HANCHU_RANGES.get(key, (None, None))
        if low != 0:
            print(f"ERROR: {key} range starts at {low}, but 0 must be accepted so a freeze survives the clamp")
            failed = True
    assert not failed, "test_hanchu_ranges_encode_the_narrow_soc_limits"


def test_clamp_range_moves_only_out_of_range_values():
    """clamp_range reports whether it had to move a value, so the caller can log it once."""
    failed = False
    cases = [
        (HANCHU_KEY_CHARGE_SOC, 80, 80, False),
        (HANCHU_KEY_CHARGE_SOC, 30, 50, True),
        (HANCHU_KEY_CHARGE_SOC, 120, 100, True),
        (HANCHU_KEY_DISCHARGE_SOC, 10, 10, False),
        (HANCHU_KEY_DISCHARGE_SOC, 60, 45, True),
        (HANCHU_KEY_DISCHARGE_SOC, 1, 5, True),
        # A freeze - rate zero - must pass through untouched, not be clamped up.
        (HANCHU_KEY_CHARGE_POWER, 0, 0, False),
        (HANCHU_KEY_CHARGE_POWER, 9000, 5000, True),
        # An unknown key has no range and is returned as-is.
        ("SOMETHING_ELSE", 1234, 1234, False),
    ]
    for key, value, expect_value, expect_clamped in cases:
        got_value, got_clamped = clamp_range(key, value)
        if got_value != expect_value or got_clamped != expect_clamped:
            print(f"ERROR: clamp_range({key}, {value}) = {(got_value, got_clamped)} != {(expect_value, expect_clamped)}")
            failed = True
    # Junk coerces to 0 and is then clamped into range like any other number, rather than raising
    # inside the write path.
    got_value, got_clamped = clamp_range(HANCHU_KEY_CHARGE_SOC, "unavailable")
    if got_value != 50 or not got_clamped:
        print(f"ERROR: clamp_range of junk = {(got_value, got_clamped)} != (50, True)")
        failed = True
    # Per-device ranges from the menu endpoint override the built-in defaults.
    got_value, got_clamped = clamp_range(HANCHU_KEY_CHARGE_POWER, 6000, {HANCHU_KEY_CHARGE_POWER: (0, 7000)})
    if got_value != 6000 or got_clamped:
        print(f"ERROR: menu-supplied range not honoured: {(got_value, got_clamped)}")
        failed = True
    assert not failed, "test_clamp_range_moves_only_out_of_range_values"


def test_time_encoding_round_trips():
    """Slots carry seconds since midnight; the two converters must agree."""
    failed = False
    for text, seconds in (("00:00:00", 0), ("01:30:00", 5400), ("23:45:00", 85500), ("02:15:00", 8100)):
        if hhmmss_to_seconds(text) != seconds:
            print(f"ERROR: hhmmss_to_seconds({text}) = {hhmmss_to_seconds(text)} != {seconds}")
            failed = True
        if seconds_to_hhmmss(seconds) != text:
            print(f"ERROR: seconds_to_hhmmss({seconds}) = {seconds_to_hhmmss(seconds)} != {text}")
            failed = True
    # Predbat spells midnight as 24:00:00. That must NOT become 0, because 0 is how a DISABLED
    # slot is written and a full-day window would collapse into nothing.
    if hhmmss_to_seconds("24:00:00") != HANCHU_SECONDS_PER_DAY - 60:
        print(f"ERROR: 24:00:00 = {hhmmss_to_seconds('24:00:00')}, should clamp to one minute before midnight")
        failed = True
    for junk in ("", None, "unavailable", "not:a:time"):
        if hhmmss_to_seconds(junk) != 0:
            print(f"ERROR: hhmmss_to_seconds({junk!r}) = {hhmmss_to_seconds(junk)} != 0")
            failed = True
        if seconds_to_hhmmss(junk) != "00:00:00":
            print(f"ERROR: seconds_to_hhmmss({junk!r}) = {seconds_to_hhmmss(junk)} != 00:00:00")
            failed = True
    assert not failed, "test_time_encoding_round_trips"


def test_split_window_seconds_splits_at_midnight():
    """A window running past midnight fills slot 1 and puts the remainder in slot 2."""
    failed = False
    first, second = split_window_seconds("01:00:00", "05:00:00")
    if first != (3600, 18000) or second != (HANCHU_SLOT_DISABLED, HANCHU_SLOT_DISABLED):
        print(f"ERROR: plain window split to {first} {second}")
        failed = True
    # 23:00 to 02:00 next day, which Predbat spells as an end of 26:00.
    first, second = split_window_seconds("23:00:00", "26:00:00")
    if first != (82800, HANCHU_SECONDS_PER_DAY - 60) or second != (HANCHU_SLOT_DISABLED, 7200):
        print(f"ERROR: midnight-spanning window split to {first} {second}")
        failed = True
    # Exactly midnight is not a wrap: it ends at the end of the day and slot 2 stays disabled.
    first, second = split_window_seconds("22:00:00", "24:00:00")
    if first != (79200, HANCHU_SECONDS_PER_DAY) or second != (HANCHU_SLOT_DISABLED, HANCHU_SLOT_DISABLED):
        print(f"ERROR: midnight-ending window split to {first} {second}")
        failed = True
    assert not failed, "test_split_window_seconds_splits_at_midnight"


def test_window_is_empty_detects_disabled_and_inverted():
    """start == end is the disabled form, and an inverted window counts as empty too."""
    failed = False
    for start, end, expect in ((0, 0, True), (3600, 3600, True), (18000, 3600, True), (3600, 18000, False)):
        got = window_is_empty(start, end)
        if got != expect:
            print(f"ERROR: window_is_empty({start}, {end}) = {got} != {expect}")
            failed = True
    assert not failed, "test_window_is_empty_detects_disabled_and_inverted"


def test_scale_to_watts_trusts_the_unit_field_over_the_heuristic():
    """The sibling unit string wins; the magnitude heuristic is only the fallback."""
    failed = False
    cases = [
        # A unit string is trusted even when it contradicts the heuristic: 2.5 kW is 2500 W, and
        # 4 W stays 4 W despite being below the heuristic's threshold.
        (2.5, "kW", 2500.0),
        (2.5, "KW", 2500.0),
        (4.0, "W", 4.0),
        (1500, "W", 1500.0),
        # No unit: below the threshold is assumed to be kW, at or above it is assumed to be W.
        (2.5, None, 2500.0),
        (1500, None, 1500.0),
        (-3.0, None, -3000.0),
        # An unrecognised unit falls back to the heuristic rather than guessing a multiplier.
        (2.5, "kilowatt", 2500.0),
    ]
    for value, unit, expect in cases:
        got = scale_to_watts(value, unit)
        if got != expect:
            print(f"ERROR: scale_to_watts({value}, {unit}) = {got} != {expect}")
            failed = True
    for junk in (None, "unavailable", ""):
        if scale_to_watts(junk) is not None:
            print(f"ERROR: scale_to_watts({junk!r}) should be None, got {scale_to_watts(junk)}")
            failed = True
    assert not failed, "test_scale_to_watts_trusts_the_unit_field_over_the_heuristic"


def test_field_maps_cover_the_predbat_args():
    """Telemetry and energy maps name every arg automatic_config binds, with the right sign flips."""
    failed = False
    for leaf in ("soc", "battery_power", "grid_power", "pv_power", "load_power"):
        if leaf not in HANCHU_TELEMETRY:
            print(f"ERROR: telemetry leaf {leaf} missing")
            failed = True
    for leaf in ("load_today", "import_today", "export_today", "pv_today"):
        if leaf not in HANCHU_ENERGY:
            print(f"ERROR: energy leaf {leaf} missing")
            failed = True
    # Hanchu reports battery positive on CHARGE and grid positive on IMPORT; Predbat's convention
    # is the opposite for both, so both must be negated and nothing else may be.
    if set(HANCHU_TELEMETRY_NEGATE) != {"battery_power", "grid_power"}:
        print(f"ERROR: negate set {HANCHU_TELEMETRY_NEGATE} != battery_power, grid_power")
        failed = True
    for leaf in HANCHU_TELEMETRY_NEGATE:
        if leaf not in HANCHU_TELEMETRY:
            print(f"ERROR: negated leaf {leaf} is not a telemetry leaf at all")
            failed = True
    assert not failed, "test_field_maps_cover_the_predbat_args"


def test_work_mode_for_schedule_is_user_defined():
    """The inferred schedule work mode is User-defined, and is named in the table.

    This is the single least well evidenced value in the component - nothing states which mode
    honours the timed slots. The test pins the deduction so that changing it is a deliberate act
    with a visible diff, not a quiet edit.
    """
    failed = False
    if HANCHU_WORK_MODES.get(HANCHU_WORK_MODE_FOR_SCHEDULE) != "User-defined":
        print(f"ERROR: schedule work mode {HANCHU_WORK_MODE_FOR_SCHEDULE} is {HANCHU_WORK_MODES.get(HANCHU_WORK_MODE_FOR_SCHEDULE)}, not User-defined")
        failed = True
    if sorted(HANCHU_WORK_MODES) != [1, 2, 3, 4]:
        print(f"ERROR: work mode values {sorted(HANCHU_WORK_MODES)} != [1, 2, 3, 4]")
        failed = True
    assert not failed, "test_work_mode_for_schedule_is_user_defined"


def run_hanchu_const_tests(my_predbat):
    """Run all Hanchu constants tests."""
    failed = False
    for name, fn in [
        ("base_url_and_endpoints", test_hanchu_base_url_and_endpoints),
        ("control_keys", test_hanchu_control_keys_cover_every_setting_predbat_writes),
        ("ranges", test_hanchu_ranges_encode_the_narrow_soc_limits),
        ("clamp_range", test_clamp_range_moves_only_out_of_range_values),
        ("time_encoding", test_time_encoding_round_trips),
        ("split_window", test_split_window_seconds_splits_at_midnight),
        ("window_is_empty", test_window_is_empty_detects_disabled_and_inverted),
        ("scale_to_watts", test_scale_to_watts_trusts_the_unit_field_over_the_heuristic),
        ("field_maps", test_field_maps_cover_the_predbat_args),
        ("work_mode", test_work_mode_for_schedule_is_user_defined),
    ]:
        try:
            if fn():
                print(f"  FAILED: hanchu_const.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in hanchu_const.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
