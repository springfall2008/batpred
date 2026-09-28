# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sungrow iSolarCloud constants
# -----------------------------------------------------------------------------

"""Tests for the Sungrow iSolarCloud constants module (sungrow_const.py)."""

from sungrow_const import (
    SUNGROW_GATEWAYS,
    SUNGROW_DEFAULT_GATEWAY,
    SUNGROW_ENDPOINTS,
    SUNGROW_PARAM_NAMES,
    SUNGROW_PARAM_SOC_UPPER,
    SUNGROW_PARAM_SOC_LOWER,
    SUNGROW_PARAM_POWER,
    SUNGROW_PARAM_HEARTBEAT,
    SUNGROW_PARAM_EMS_MODE,
    SUNGROW_PARAM_COMMAND,
    SUNGROW_PARAM_SCALE_UNVERIFIED,
    SUNGROW_HEARTBEAT_MIN_SEND_SECONDS,
    SUNGROW_HEARTBEAT_MARGIN,
    SUNGROW_ESS_POINTS,
    SUNGROW_BATTERY_POINTS,
    SUNGROW_METER_POINTS,
    SUNGROW_PLANT_POINTS,
    SUNGROW_PLANT_TELEMETRY,
    SUNGROW_PLANT_ENERGY,
    SUNGROW_CMD_CHARGE,
    SUNGROW_CMD_DISCHARGE,
    SUNGROW_CMD_STOP,
    SUNGROW_AUTH_ERROR_CODES,
    SUNGROW_RATE_LIMIT_CODES,
    SUNGROW_MAX_POINTS_PER_REQUEST,
    gateway_url,
    strip_point_prefix,
    as_float,
    clamp_range,
    scale_for_write,
    scale_from_read,
    heartbeat_send_interval,
    signed_battery_power,
    signed_grid_power,
    hhmmss_to_hour_minute,
    hm_to_minutes,
    window_is_empty,
    param_entry,
    describe_param,
)


def test_sungrow_gateways_and_endpoints():
    """Every documented regional gateway is present and every endpoint hangs off /openapi/platform."""
    failed = False
    for region in ("china", "international", "europe", "australia", "india"):
        if region not in SUNGROW_GATEWAYS:
            print(f"ERROR: gateway region {region} missing")
            failed = True
    # Europe is the default because that is where the UK fleet lives; pointing at the wrong
    # region authenticates and then finds no plants.
    if SUNGROW_DEFAULT_GATEWAY != "https://gateway.isolarcloud.eu":
        print(f"ERROR: default gateway {SUNGROW_DEFAULT_GATEWAY}")
        failed = True
    for endpoint in ("plant_list", "plant_detail", "device_list", "plant_realtime", "device_realtime", "param_check", "param_setting", "param_task"):
        path = SUNGROW_ENDPOINTS.get(endpoint)
        if not path:
            print(f"ERROR: endpoint {endpoint} missing")
            failed = True
            continue
        if not path.startswith("/openapi/platform/"):
            print(f"ERROR: endpoint {endpoint} path {path} is not under /openapi/platform/")
            failed = True
    assert not failed, "test_sungrow_gateways_and_endpoints"


def test_gateway_url_accepts_region_or_url():
    """A region name and a full URL both resolve, and an unknown value is not silently defaulted."""
    failed = False
    if gateway_url("europe") != "https://gateway.isolarcloud.eu":
        print("ERROR: region name did not resolve")
        failed = True
    if gateway_url("EUROPE") != "https://gateway.isolarcloud.eu":
        print("ERROR: region name is case sensitive")
        failed = True
    if gateway_url("") != SUNGROW_DEFAULT_GATEWAY:
        print("ERROR: empty gateway did not fall back to the default")
        failed = True
    if gateway_url("https://gateway.isolarcloud.in/") != "https://gateway.isolarcloud.in":
        print("ERROR: trailing slash not stripped from an explicit URL")
        failed = True
    # Deliberately NOT replaced with the default: silently sending a misconfigured account to
    # Europe reads as an empty account rather than as a configuration error.
    if gateway_url("https://example.invalid") != "https://example.invalid":
        print("ERROR: an unrecognised gateway should pass through unchanged")
        failed = True
    assert not failed, "test_gateway_url_accepts_region_or_url"


def test_control_parameter_table_covers_appendix_10():
    """Every parameter the component writes, and the C&I equivalents, carry a readable name."""
    failed = False
    for code in ("10001", "10002", "10003", "10004", "10005", "10017", "10065", "10066", "10067", "10068", "10069", "10070", "10071", "10072", "10073", "10074", "10075", "10076", "10082", "10085", "10091", "10092"):
        if code not in SUNGROW_PARAM_NAMES:
            print(f"ERROR: parameter {code} missing from the name table")
            failed = True
    if describe_param("10017") != "10017 (external_ems_heartbeat)":
        print(f"ERROR: describe_param {describe_param('10017')}")
        failed = True
    if describe_param("99999") != "99999":
        print("ERROR: an unknown parameter should describe as the bare code")
        failed = True
    assert not failed, "test_control_parameter_table_covers_appendix_10"


def test_soc_limit_scaling_matches_the_documented_ranges():
    """700-1000 == 70-100% and 0-500 == 0-50% pin both SoC limits at a factor of ten.

    These two coefficients are the only ones Appendix 10 effectively states, via the accepted
    ranges it publishes. Everything else in the scale table is an assumption, which is why the
    test asserts these specifically rather than the table as a whole.
    """
    failed = False
    if SUNGROW_PARAM_SCALE_UNVERIFIED.get(SUNGROW_PARAM_SOC_UPPER) != 10:
        print("ERROR: SoC upper limit scale is not 10")
        failed = True
    if SUNGROW_PARAM_SCALE_UNVERIFIED.get(SUNGROW_PARAM_SOC_LOWER) != 10:
        print("ERROR: SoC lower limit scale is not 10")
        failed = True
    if scale_for_write(SUNGROW_PARAM_SOC_UPPER, 90) != "900":
        print(f"ERROR: 90% upper limit wrote {scale_for_write(SUNGROW_PARAM_SOC_UPPER, 90)}, expected 900")
        failed = True
    if scale_for_write(SUNGROW_PARAM_SOC_LOWER, 20) != "200":
        print(f"ERROR: 20% lower limit wrote {scale_for_write(SUNGROW_PARAM_SOC_LOWER, 20)}, expected 200")
        failed = True
    # Watts go out unscaled - 10005's documented range is 0-5000 W.
    if scale_for_write(SUNGROW_PARAM_POWER, 3000) != "3000":
        print(f"ERROR: 3000 W wrote {scale_for_write(SUNGROW_PARAM_POWER, 3000)}")
        failed = True
    assert not failed, "test_soc_limit_scaling_matches_the_documented_ranges"


def test_scale_for_write_clamps_to_the_documented_range():
    """An out-of-range value is trimmed, not sent.

    An out-of-range write is refused outright by the cloud, which leaves the inverter running
    the PREVIOUS setpoint - so a trimmed setpoint is much closer to the plan than a stale one.
    """
    failed = False
    # 10001 accepts 70-100%; a Predbat target of 50% has to become 70%.
    if scale_for_write(SUNGROW_PARAM_SOC_UPPER, 50) != "700":
        print(f"ERROR: 50% upper limit wrote {scale_for_write(SUNGROW_PARAM_SOC_UPPER, 50)}, expected the 70% floor")
        failed = True
    if scale_for_write(SUNGROW_PARAM_SOC_UPPER, 150) != "1000":
        print("ERROR: an over-range upper limit was not clamped to 100%")
        failed = True
    # 10002 accepts 0-50%.
    if scale_for_write(SUNGROW_PARAM_SOC_LOWER, 80) != "500":
        print("ERROR: an over-range lower limit was not clamped to 50%")
        failed = True
    # 10005 accepts 0-5000 W.
    if scale_for_write(SUNGROW_PARAM_POWER, 9000) != "5000":
        print("ERROR: an over-range power was not clamped")
        failed = True
    if scale_for_write(SUNGROW_PARAM_POWER, -100) != "0":
        print("ERROR: a negative power was not clamped to zero")
        failed = True
    # A parameter with no documented range passes through untouched.
    if clamp_range(SUNGROW_PARAM_COMMAND, 187) != 187:
        print("ERROR: a parameter with no documented range was clamped")
        failed = True
    assert not failed, "test_scale_for_write_clamps_to_the_documented_range"


def test_scale_for_write_emits_integral_strings():
    """set_value is a string, and an integral result loses its .0 to match the portal's examples."""
    failed = False
    value = scale_for_write(SUNGROW_PARAM_COMMAND, SUNGROW_CMD_CHARGE)
    if not isinstance(value, str):
        print("ERROR: set_value is not a string")
        failed = True
    if value != "170":
        print(f"ERROR: charge command wrote {value}")
        failed = True
    if scale_for_write(SUNGROW_PARAM_EMS_MODE, 3) != "3":
        print("ERROR: EMS mode did not write as a bare integer")
        failed = True
    assert not failed, "test_scale_for_write_emits_integral_strings"


def test_scale_from_read_inverts_scale_for_write():
    """A read-back can be compared against what was asked for, and an unreadable value is None."""
    failed = False
    if scale_from_read(SUNGROW_PARAM_SOC_UPPER, "900") != 90:
        print(f"ERROR: 900 read back as {scale_from_read(SUNGROW_PARAM_SOC_UPPER, '900')}")
        failed = True
    # None rather than 0: a parameter that could not be read is not one that reads zero.
    if scale_from_read(SUNGROW_PARAM_SOC_UPPER, "--") is not None:
        print("ERROR: an unreadable value should be None, not a number")
        failed = True
    assert not failed, "test_scale_from_read_inverts_scale_for_write"


def test_heartbeat_send_interval_beats_inside_the_promised_window():
    """Beats go out well inside the declared interval and never faster than the floor."""
    failed = False
    interval = heartbeat_send_interval(300)
    if interval >= 300:
        print(f"ERROR: send interval {interval} is not inside the promised 300s window")
        failed = True
    if interval != 300 * SUNGROW_HEARTBEAT_MARGIN:
        print(f"ERROR: send interval {interval} does not apply the margin")
        failed = True
    # The documented minimum interval is 1 second; without the floor that would be a dispatch
    # every half second forever, which is a self-inflicted rate limit rather than tighter control.
    if heartbeat_send_interval(1) != SUNGROW_HEARTBEAT_MIN_SEND_SECONDS:
        print(f"ERROR: a 1s interval gave a send cadence of {heartbeat_send_interval(1)}")
        failed = True
    # 10017's documented range is 1-1000s, so an absurd config is clamped before the margin.
    if heartbeat_send_interval(99999) != 1000 * SUNGROW_HEARTBEAT_MARGIN:
        print(f"ERROR: an over-range interval gave {heartbeat_send_interval(99999)}")
        failed = True
    assert not failed, "test_heartbeat_send_interval_beats_inside_the_promised_window"


def test_strip_point_prefix_separates_points_from_metadata():
    """p13141 is a point; time_stamp and ps_id are not."""
    failed = False
    if strip_point_prefix("p13141") != "13141":
        print("ERROR: point prefix not stripped")
        failed = True
    for key in ("time_stamp", "ps_id", "device_sn", "p", "", None, "point_dict"):
        if strip_point_prefix(key) is not None:
            print(f"ERROR: {key!r} was read as a measuring point")
            failed = True
    assert not failed, "test_strip_point_prefix_separates_points_from_metadata"


def test_as_float_tolerates_the_apis_unavailable_marker():
    """iSolarCloud writes an unavailable point as '--' rather than omitting it."""
    failed = False
    if as_float("--") is not None:
        print("ERROR: '--' did not read as unavailable")
        failed = True
    if as_float(None) is not None:
        print("ERROR: None did not read as unavailable")
        failed = True
    if as_float("1234.5") != 1234.5:
        print("ERROR: a numeric string did not parse")
        failed = True
    if as_float("--", 0.0) != 0.0:
        print("ERROR: the default was not honoured")
        failed = True
    assert not failed, "test_as_float_tolerates_the_apis_unavailable_marker"


def test_signed_battery_power_combines_the_two_unsigned_halves():
    """Positive on discharge, negative on charge, and None when neither point was reported."""
    failed = False
    if signed_battery_power(3000, 0) != -3000:
        print(f"ERROR: charging read as {signed_battery_power(3000, 0)}, expected -3000")
        failed = True
    if signed_battery_power(0, 2500) != 2500:
        print(f"ERROR: discharging read as {signed_battery_power(0, 2500)}, expected 2500")
        failed = True
    # A model that reports only one of the two halves still produces a usable sensor.
    if signed_battery_power(None, 1000) != 1000:
        print("ERROR: a single reported half did not produce a value")
        failed = True
    # Neither reported is absent, not zero: a fabricated 0 W is indistinguishable from a real one.
    if signed_battery_power(None, None) is not None:
        print("ERROR: an unreported battery power should be None, not 0")
        failed = True
    assert not failed, "test_signed_battery_power_combines_the_two_unsigned_halves"


def test_signed_grid_power_is_negative_on_import():
    """Predbat's grid_power is negative on import and positive on export.

    Getting this backwards makes every export read as an import and silently inverts the whole
    plan, which is exactly why it has its own test rather than being asserted in passing.
    """
    failed = False
    if signed_grid_power(4000, 0) != -4000:
        print(f"ERROR: importing 4000 W read as {signed_grid_power(4000, 0)}, expected -4000")
        failed = True
    if signed_grid_power(0, 1500) != 1500:
        print(f"ERROR: exporting 1500 W read as {signed_grid_power(0, 1500)}, expected 1500")
        failed = True
    if signed_grid_power(None, None) is not None:
        print("ERROR: an unreported grid power should be None, not 0")
        failed = True
    assert not failed, "test_signed_grid_power_is_negative_on_import"


def test_hhmmss_to_hour_minute_splits_for_the_window_parameters():
    """The forced-charging window takes hour and minute as separate parameter codes."""
    failed = False
    if hhmmss_to_hour_minute("02:30:00") != (2, 30):
        print(f"ERROR: 02:30:00 split as {hhmmss_to_hour_minute('02:30:00')}")
        failed = True
    if hhmmss_to_hour_minute("23:59:00") != (23, 59):
        print("ERROR: 23:59:00 did not split")
        failed = True
    # Predbat's midnight end. The parameters cannot express 24:00, and wrapping it to 00:00
    # would read as an empty window and cancel the charge.
    if hhmmss_to_hour_minute("24:00:00") != (23, 59):
        print(f"ERROR: 24:00:00 split as {hhmmss_to_hour_minute('24:00:00')}, expected (23, 59)")
        failed = True
    for junk in ("", None, "nonsense", "unknown"):
        if hhmmss_to_hour_minute(junk) != (0, 0):
            print(f"ERROR: {junk!r} did not fall back to (0, 0)")
            failed = True
    assert not failed, "test_hhmmss_to_hour_minute_splits_for_the_window_parameters"


def test_hm_to_minutes_keeps_hours_above_midnight():
    """Predbat represents a window crossing midnight as 23:00-25:00, so 25:00 must survive."""
    failed = False
    if hm_to_minutes("02:30:00") != 150:
        print("ERROR: 02:30:00 did not convert")
        failed = True
    if hm_to_minutes("25:00:00") != 1500:
        print(f"ERROR: 25:00:00 converted to {hm_to_minutes('25:00:00')}, expected 1500")
        failed = True
    if hm_to_minutes("junk") != 0:
        print("ERROR: junk did not fall back to 0")
        failed = True
    assert not failed, "test_hm_to_minutes_keeps_hours_above_midnight"


def test_window_is_empty_detects_disabled_and_inverted_windows():
    """An inverted window is disabled, not written as an undocumented wrap-around."""
    failed = False
    if not window_is_empty("02:00:00", "02:00:00"):
        print("ERROR: a zero-length window was not empty")
        failed = True
    if not window_is_empty("22:00:00", "02:00:00"):
        print("ERROR: an inverted window was not empty - it must not be written as a wrap-around")
        failed = True
    if window_is_empty("02:00:00", "05:00:00"):
        print("ERROR: a normal window read as empty")
        failed = True
    assert not failed, "test_window_is_empty_detects_disabled_and_inverted_windows"


def test_param_entry_shape():
    """A param_list entry carries param_code and set_value, both as strings."""
    failed = False
    entry = param_entry("10005", "3000")
    if entry != {"param_code": "10005", "set_value": "3000"}:
        print(f"ERROR: param entry {entry}")
        failed = True
    # A read carries an empty set_value, which is the only structural difference from a write.
    if param_entry("10005") != {"param_code": "10005", "set_value": ""}:
        print("ERROR: a read entry did not default to an empty set_value")
        failed = True
    assert not failed, "test_param_entry_shape"


def test_measuring_point_maps_are_disjoint_and_keyed_by_string():
    """Point ids are strings because that is how point_dict keys them, and the maps do not collide."""
    failed = False
    for name, point_map in (("ess", SUNGROW_ESS_POINTS), ("battery", SUNGROW_BATTERY_POINTS), ("meter", SUNGROW_METER_POINTS), ("plant", SUNGROW_PLANT_POINTS)):
        for point_id in point_map:
            if not isinstance(point_id, str) or not point_id.isdigit():
                print(f"ERROR: {name} point id {point_id!r} is not a numeric string")
                failed = True
    overlap = set(SUNGROW_ESS_POINTS) & set(SUNGROW_PLANT_POINTS)
    if overlap:
        print(f"ERROR: device and plant point ids overlap: {overlap}")
        failed = True
    assert not failed, "test_measuring_point_maps_are_disjoint_and_keyed_by_string"


def test_plant_maps_reference_real_points():
    """Every leaf the telemetry and energy maps name exists in the plant point map.

    A typo here is silent: telemetry() would simply never find the value and Predbat would run
    without PV or without the daily counters, with nothing logged.
    """
    failed = False
    for leaf, point_leaf in list(SUNGROW_PLANT_TELEMETRY.items()) + list(SUNGROW_PLANT_ENERGY.items()):
        if point_leaf not in SUNGROW_PLANT_POINTS.values():
            print(f"ERROR: {leaf} maps to {point_leaf}, which is not a plant point")
            failed = True
    for required in ("pv_power", "load_power", "grid_power", "soc"):
        if required not in SUNGROW_PLANT_TELEMETRY:
            print(f"ERROR: plant telemetry has no fallback for {required}")
            failed = True
    for required in ("load_today", "import_today", "export_today", "pv_today"):
        if required not in SUNGROW_PLANT_ENERGY:
            print(f"ERROR: plant energy has no {required}")
            failed = True
    assert not failed, "test_plant_maps_reference_real_points"


def test_ess_points_carry_the_values_the_control_path_needs():
    """The energy-storage inverter map has SoC and both unsigned power halves."""
    failed = False
    for point_id, leaf in (("13141", "battery_soc"), ("13126", "battery_charge_power"), ("13150", "battery_discharge_power"), ("13119", "load_power"), ("13121", "feed_in_power"), ("13149", "purchased_power")):
        if SUNGROW_ESS_POINTS.get(point_id) != leaf:
            print(f"ERROR: ESS point {point_id} maps to {SUNGROW_ESS_POINTS.get(point_id)}, expected {leaf}")
            failed = True
    assert not failed, "test_ess_points_carry_the_values_the_control_path_needs"


def test_command_values_are_the_documented_magic_bytes():
    """170/187/204 are what Appendix 10 publishes for charge, discharge and stop."""
    failed = False
    if (SUNGROW_CMD_CHARGE, SUNGROW_CMD_DISCHARGE, SUNGROW_CMD_STOP) != (170, 187, 204):
        print(f"ERROR: command values {(SUNGROW_CMD_CHARGE, SUNGROW_CMD_DISCHARGE, SUNGROW_CMD_STOP)}")
        failed = True
    assert not failed, "test_command_values_are_the_documented_magic_bytes"


def test_heartbeat_parameter_is_10017():
    """The dead-man's switch is 10017; writing any other parameter does not hold control."""
    failed = False
    if SUNGROW_PARAM_HEARTBEAT != "10017":
        print(f"ERROR: heartbeat parameter is {SUNGROW_PARAM_HEARTBEAT}")
        failed = True
    assert not failed, "test_heartbeat_parameter_is_10017"


def test_error_code_groups_are_short_and_disjoint():
    """These are the few codes observed in the wild, not an invented appendix.

    The portal's full error table could not be read, so the component reports the server's own
    result_msg for everything else. Keeping these groups small and separate is the point: an
    over-broad auth group would blame the credentials for an unrelated failure.
    """
    failed = False
    if set(SUNGROW_AUTH_ERROR_CODES) & set(SUNGROW_RATE_LIMIT_CODES):
        print("ERROR: the auth and rate-limit code groups overlap")
        failed = True
    for code in SUNGROW_AUTH_ERROR_CODES + SUNGROW_RATE_LIMIT_CODES:
        if not isinstance(code, str) or not code.startswith("E"):
            print(f"ERROR: error code {code!r} is not an E-prefixed string as the API returns them")
            failed = True
    # getDeviceRealTimeData answers result_code 010 above this, which looks like an unsupported
    # endpoint rather than an oversized request - so the maps must stay under it.
    if SUNGROW_MAX_POINTS_PER_REQUEST != 100:
        print(f"ERROR: the documented point cap is {SUNGROW_MAX_POINTS_PER_REQUEST}, expected 100")
        failed = True
    for name, point_map in (("ess", SUNGROW_ESS_POINTS), ("battery", SUNGROW_BATTERY_POINTS), ("meter", SUNGROW_METER_POINTS), ("plant", SUNGROW_PLANT_POINTS)):
        if len(point_map) > SUNGROW_MAX_POINTS_PER_REQUEST:
            print(f"ERROR: the {name} point map has {len(point_map)} points, above the documented cap")
            failed = True
    assert not failed, "test_error_code_groups_are_short_and_disjoint"


def run_sungrow_const_tests(my_predbat):
    """Run all Sungrow constants tests."""
    failed = False
    for name, fn in [
        ("gateways_and_endpoints", test_sungrow_gateways_and_endpoints),
        ("gateway_url", test_gateway_url_accepts_region_or_url),
        ("parameter_table", test_control_parameter_table_covers_appendix_10),
        ("soc_scaling", test_soc_limit_scaling_matches_the_documented_ranges),
        ("scale_clamps", test_scale_for_write_clamps_to_the_documented_range),
        ("scale_strings", test_scale_for_write_emits_integral_strings),
        ("scale_from_read", test_scale_from_read_inverts_scale_for_write),
        ("heartbeat_interval", test_heartbeat_send_interval_beats_inside_the_promised_window),
        ("strip_point_prefix", test_strip_point_prefix_separates_points_from_metadata),
        ("as_float", test_as_float_tolerates_the_apis_unavailable_marker),
        ("signed_battery_power", test_signed_battery_power_combines_the_two_unsigned_halves),
        ("signed_grid_power", test_signed_grid_power_is_negative_on_import),
        ("hhmmss_split", test_hhmmss_to_hour_minute_splits_for_the_window_parameters),
        ("hm_to_minutes", test_hm_to_minutes_keeps_hours_above_midnight),
        ("window_is_empty", test_window_is_empty_detects_disabled_and_inverted_windows),
        ("param_entry", test_param_entry_shape),
        ("point_maps", test_measuring_point_maps_are_disjoint_and_keyed_by_string),
        ("plant_maps", test_plant_maps_reference_real_points),
        ("ess_points", test_ess_points_carry_the_values_the_control_path_needs),
        ("command_values", test_command_values_are_the_documented_magic_bytes),
        ("heartbeat_param", test_heartbeat_parameter_is_10017),
        ("error_code_groups", test_error_code_groups_are_short_and_disjoint),
    ]:
        try:
            if fn():
                print(f"  FAILED: sungrow_const.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sungrow_const.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
