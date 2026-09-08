# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk Cloud constants
# -----------------------------------------------------------------------------

"""Tests for the Sunsynk Cloud constants module (sunsynk_const.py)."""

from sunsynk_const import (
    SUNSYNK_REGIONS,
    SUNSYNK_ENDPOINTS,
    SUNSYNK_WORKMODE,
    SUNSYNK_TELEMETRY,
    SUNSYNK_ENERGY,
    SUNSYNK_BOOL_FIELDS,
    SUNSYNK_DAY_FIELDS,
    TOU_FIELD,
    TOU_SLOT_COUNT,
    TOU_FILLER_TIMES,
    FREEZE_EXPORT_SOC,
    encode_setting,
)


def test_sunsynk_regions():
    """Both regions expose an https host and the source token the login signs with."""
    failed = False
    for region, expect_source in (("sunsynk", "sunsynk"), ("inteless", "elinter")):
        entry = SUNSYNK_REGIONS.get(region)
        if not entry:
            print(f"ERROR: region {region} missing")
            failed = True
            continue
        if not entry.get("host", "").startswith("https://"):
            print(f"ERROR: region {region} host not https: {entry.get('host')}")
            failed = True
        if entry.get("source") != expect_source:
            print(f"ERROR: region {region} source {entry.get('source')} != {expect_source}")
            failed = True
    assert not failed, "test_sunsynk_regions"


def test_sunsynk_endpoints():
    """Every endpoint the component calls is declared, and templated ones carry {sn}."""
    failed = False
    for endpoint in ("public_key", "token", "token_legacy", "inverter_list", "inverter_detail", "battery", "grid", "load", "input", "settings_read", "settings_set"):
        if endpoint not in SUNSYNK_ENDPOINTS:
            print(f"ERROR: endpoint {endpoint} missing")
            failed = True
    for endpoint in ("inverter_detail", "battery", "grid", "load", "input", "settings_read", "settings_set"):
        if "{sn}" not in SUNSYNK_ENDPOINTS.get(endpoint, ""):
            print(f"ERROR: endpoint {endpoint} has no {{sn}} placeholder")
            failed = True
    assert not failed, "test_sunsynk_endpoints"


def test_sunsynk_workmode_semantics():
    """The three DEYE-equivalent work modes exist and have distinct wire values."""
    failed = False
    for mode in ("selling_first", "zero_export_load", "zero_export_ct"):
        if mode not in SUNSYNK_WORKMODE:
            print(f"ERROR: work mode {mode} missing")
            failed = True
    values = list(SUNSYNK_WORKMODE.values())
    if len(set(values)) != len(values):
        print(f"ERROR: work mode wire values not distinct: {values}")
        failed = True
    assert not failed, "test_sunsynk_workmode_semantics"


def test_tou_field_templates():
    """Each per-slot field is a template that renders a distinct name for all 6 slots."""
    failed = False
    if TOU_SLOT_COUNT != 6:
        print(f"ERROR: TOU_SLOT_COUNT must be 6, got {TOU_SLOT_COUNT}")
        failed = True
    for concept in ("time", "power", "soc", "grid_charge"):
        template = TOU_FIELD.get(concept)
        if not template:
            print(f"ERROR: TOU_FIELD {concept} missing")
            failed = True
            continue
        if "{n}" not in template:
            print(f"ERROR: TOU_FIELD {concept} is not a template: {template}")
            failed = True
            continue
        names = [template.format(n=n) for n in range(1, TOU_SLOT_COUNT + 1)]
        if len(set(names)) != TOU_SLOT_COUNT:
            print(f"ERROR: TOU_FIELD {concept} does not render 6 distinct names: {names}")
            failed = True
    assert not failed, "test_tou_field_templates"


def test_tou_filler_times_sufficient():
    """There are enough distinct filler times to pad any schedule out to 6 slots."""
    failed = False
    if len(set(TOU_FILLER_TIMES)) != len(TOU_FILLER_TIMES):
        print(f"ERROR: filler times not distinct: {TOU_FILLER_TIMES}")
        failed = True
    # A schedule contributes at most 4 boundary times (charge start/end, export start/end),
    # so padding needs TOU_SLOT_COUNT distinct fillers to survive every one colliding.
    if len(TOU_FILLER_TIMES) <= TOU_SLOT_COUNT:
        print(f"ERROR: need more than {TOU_SLOT_COUNT} filler times, got {len(TOU_FILLER_TIMES)}")
        failed = True
    if TOU_FILLER_TIMES != sorted(TOU_FILLER_TIMES):
        print(f"ERROR: filler times not ascending: {TOU_FILLER_TIMES}")
        failed = True
    if TOU_FILLER_TIMES[0] != "00:00":
        print(f"ERROR: first filler time must be 00:00, got {TOU_FILLER_TIMES[0]}")
        failed = True
    assert not failed, "test_tou_filler_times_sufficient"


def test_telemetry_maps_cover_predbat_args():
    """Every sensor Predbat needs is sourced from a declared endpoint and field."""
    failed = False
    valid_sources = ("battery", "grid", "load", "input", "detail")
    for leaf in ("soc", "battery_power", "grid_power", "load_power", "pv_power", "temperature"):
        if leaf not in SUNSYNK_TELEMETRY:
            print(f"ERROR: telemetry {leaf} missing")
            failed = True
    for leaf in ("pv_today", "import_today", "export_today", "load_today", "battery_charge_today", "battery_discharge_today"):
        if leaf not in SUNSYNK_ENERGY:
            print(f"ERROR: energy counter {leaf} missing")
            failed = True
    for name, mapping in list(SUNSYNK_TELEMETRY.items()) + list(SUNSYNK_ENERGY.items()):
        source, field = mapping
        if source not in valid_sources:
            print(f"ERROR: {name} reads from unknown source {source}")
            failed = True
        if not field:
            print(f"ERROR: {name} has an empty field name")
            failed = True
    assert not failed, "test_telemetry_maps_cover_predbat_args"


def test_encode_setting_types():
    """Boolean fields serialise bare, everything else serialises as a string."""
    failed = False
    # All six grid-charge flags and all seven day flags must be declared boolean.
    for n in range(1, TOU_SLOT_COUNT + 1):
        name = TOU_FIELD["grid_charge"].format(n=n)
        if name not in SUNSYNK_BOOL_FIELDS:
            print(f"ERROR: {name} must be declared a boolean field")
            failed = True
    if len(SUNSYNK_DAY_FIELDS) != 7:
        print(f"ERROR: expected 7 day fields, got {len(SUNSYNK_DAY_FIELDS)}")
        failed = True
    for day in SUNSYNK_DAY_FIELDS:
        if day not in SUNSYNK_BOOL_FIELDS:
            print(f"ERROR: day field {day} must be declared a boolean field")
            failed = True
    # Booleans are sent QUOTED - a bare JSON boolean is silently discarded by the API.
    cases = [
        ("time1on", True, "true"),
        ("time1on", "true", "true"),
        ("time1on", 1, "true"),
        ("time1on", False, "false"),
        ("time1on", "false", "false"),
        ("time1on", 0, "false"),
        ("mondayOn", True, "true"),
        ("cap1", 95, "95"),
        ("cap1", "95", "95"),
        ("sellTime1", "02:00", "02:00"),
        ("sellTime1Pac", 3000, "3000"),
        ("sysWorkMode", "1", "1"),
    ]
    for name, value, expect in cases:
        got = encode_setting(name, value)
        if got != expect or not isinstance(got, type(expect)):
            print(f"ERROR: encode_setting({name!r}, {value!r}) = {got!r} ({type(got).__name__}), expected {expect!r} ({type(expect).__name__})")
            failed = True
    assert not failed, "test_encode_setting_types"


def test_freeze_export_soc():
    """Freeze-export holds at 99% so the battery neither charges nor discharges."""
    failed = False
    if FREEZE_EXPORT_SOC != 99:
        print(f"ERROR: FREEZE_EXPORT_SOC must be 99, got {FREEZE_EXPORT_SOC}")
        failed = True
    assert not failed, "test_freeze_export_soc"


def run_sunsynk_const_tests(my_predbat):
    """Run all Sunsynk constants tests."""
    failed = False
    for name, fn in [
        ("regions", test_sunsynk_regions),
        ("endpoints", test_sunsynk_endpoints),
        ("workmode_semantics", test_sunsynk_workmode_semantics),
        ("tou_field_templates", test_tou_field_templates),
        ("tou_filler_times", test_tou_filler_times_sufficient),
        ("telemetry_maps", test_telemetry_maps_cover_predbat_args),
        ("encode_setting", test_encode_setting_types),
        ("freeze_export_soc", test_freeze_export_soc),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_const.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_const.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
