# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test AlphaESS Cloud constants
# -----------------------------------------------------------------------------

"""Tests for the AlphaESS Cloud constants module (alphaess_const.py)."""

from alphaess_const import (
    ALPHAESS_BASE_URL,
    ALPHAESS_ENDPOINTS,
    ALPHAESS_RETURN_CODES,
    ALPHAESS_TELEMETRY,
    ALPHAESS_ENERGY,
    ALPHAESS_HISTORY,
    ALPHAESS_AC_COUPLED_MODELS,
    snap_time_grid,
    hhmmss_to_hhmm,
    hm_to_minutes,
    window_is_empty,
)


def test_alphaess_base_url_and_endpoints():
    """Every endpoint the component calls is declared and hangs off the documented host."""
    failed = False
    if ALPHAESS_BASE_URL != "https://openapi.alphaess.com/api":
        print(f"ERROR: base url {ALPHAESS_BASE_URL}")
        failed = True
    for endpoint in (
        "ess_list",
        "last_power",
        "one_day_power",
        "one_date_energy",
        "sum_data",
        "charge_config",
        "update_charge_config",
        "discharge_config",
        "update_discharge_config",
        "time_charge",
        "set_time_charge",
        "verification_code",
        "bind",
        "unbind",
    ):
        if endpoint not in ALPHAESS_ENDPOINTS:
            print(f"ERROR: endpoint {endpoint} missing")
            failed = True
    assert not failed, "test_alphaess_base_url_and_endpoints"


def test_alphaess_return_codes_cover_the_documented_table():
    """Every code the component branches on carries a human description."""
    failed = False
    for code in (6001, 6002, 6003, 6004, 6005, 6006, 6007, 6008, 6009, 6010, 6012, 6017, 6038, 6042, 6046, 6053):
        if code not in ALPHAESS_RETURN_CODES:
            print(f"ERROR: return code {code} missing")
            failed = True
    assert not failed, "test_alphaess_return_codes_cover_the_documented_table"


def test_snap_time_grid_rounds_inward():
    """Windows snap INWARD - start up, end down - so Predbat never claims time the device ignores.

    Off-grid values are accepted by the API and silently ignored by the inverter, which is
    the worst possible failure mode, so this is enforced here rather than hoped for.
    """
    failed = False
    cases = [
        ("00:00", "start", "00:00"),
        ("01:07", "start", "01:15"),
        ("01:15", "start", "01:15"),
        ("05:01", "end", "05:00"),
        ("05:45", "end", "05:45"),
        ("23:59", "end", "23:45"),
        ("24:00", "end", "23:45"),
        ("23:50", "start", "23:45"),
    ]
    for value, direction, expect in cases:
        got = snap_time_grid(value, direction)
        if got != expect:
            print(f"ERROR: snap_time_grid({value!r}, {direction!r}) = {got} != {expect}")
            failed = True
    assert not failed, "test_snap_time_grid_rounds_inward"


def test_hhmmss_to_hhmm():
    """Predbat's HH:MM:SS entities convert to the API's HH:mm."""
    failed = False
    for value, expect in (("01:30:00", "01:30"), ("01:30", "01:30"), ("", "00:00"), (None, "00:00")):
        got = hhmmss_to_hhmm(value)
        if got != expect:
            print(f"ERROR: hhmmss_to_hhmm({value!r}) = {got} != {expect}")
            failed = True
    assert not failed, "test_hhmmss_to_hhmm"


def test_window_is_empty_detects_disabled_and_collapsed():
    """start == end is the documented 'disabled', and an inverted window counts as empty too."""
    failed = False
    for start, end, expect in (("00:00", "00:00", True), ("01:00", "01:00", True), ("05:00", "04:00", True), ("01:00", "02:00", False)):
        got = window_is_empty(start, end)
        if got != expect:
            print(f"ERROR: window_is_empty({start}, {end}) = {got} != {expect}")
            failed = True
    if hm_to_minutes("02:15") != 135:
        print("ERROR: hm_to_minutes('02:15') != 135")
        failed = True
    assert not failed, "test_window_is_empty_detects_disabled_and_collapsed"


def test_field_maps_cover_the_predbat_args():
    """Telemetry, energy and history maps name every arg automatic_config binds."""
    failed = False
    for leaf in ("soc", "battery_power", "grid_power", "pv_power", "load_power"):
        if leaf not in ALPHAESS_TELEMETRY:
            print(f"ERROR: telemetry leaf {leaf} missing")
            failed = True
    for leaf in ("import_today", "export_today", "pv_today"):
        if leaf not in ALPHAESS_ENERGY:
            print(f"ERROR: energy leaf {leaf} missing")
            failed = True
    for leaf in ("soc", "pv_power", "load_power"):
        if leaf not in ALPHAESS_HISTORY:
            print(f"ERROR: history leaf {leaf} missing")
            failed = True
    # Ships empty on purpose: AlphaESS model names do not encode coupling, so an invented
    # table would be a guess. Entries are added only as testers confirm them.
    if ALPHAESS_AC_COUPLED_MODELS:
        print(f"ERROR: ALPHAESS_AC_COUPLED_MODELS should ship empty, got {ALPHAESS_AC_COUPLED_MODELS}")
        failed = True
    assert not failed, "test_field_maps_cover_the_predbat_args"


def run_alphaess_const_tests(my_predbat):
    """Run all AlphaESS constants tests."""
    failed = False
    for name, fn in [
        ("base_url_and_endpoints", test_alphaess_base_url_and_endpoints),
        ("return_codes", test_alphaess_return_codes_cover_the_documented_table),
        ("snap_time_grid", test_snap_time_grid_rounds_inward),
        ("hhmmss_to_hhmm", test_hhmmss_to_hhmm),
        ("window_is_empty", test_window_is_empty_detects_disabled_and_collapsed),
        ("field_maps", test_field_maps_cover_the_predbat_args),
    ]:
        try:
            if fn():
                print(f"  FAILED: alphaess_const.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in alphaess_const.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
