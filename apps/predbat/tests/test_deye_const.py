# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt on

"""Tests for the DEYE Cloud constants module (deye_const.py)."""

from deye_const import DEYE_BASE_URLS, DEYE_ENDPOINTS, DEYE_WORKMODE, DEYE_TELEMETRY_KEYS, TOU_FIELD, TOU_SLOT_COUNT, FREEZE_EXPORT_SOC


# The per-slot wire names, transcribed from definitions.TimeUseSettingItem in DEYE's
# published Swagger (GET https://eu1-developer.deyecloud.com/v2/api-docs). Spelled out here
# rather than derived from TOU_FIELD on purpose: a test that reads the same dict it is
# checking follows a typo or a dropped entry straight into the payload without complaint.
# A wrong per-slot key is silent - the write is accepted and what the API did not recognise
# is discarded - which is how enableSell came to be missing in the first place.
#
# "voltage" is in the model but deliberately not written (battery voltage mode; Predbat
# drives SOC targets), so its absence is part of what this pins.
DEYE_TOU_WIRE_FIELDS = {
    "time": "time",
    "power": "power",
    "soc": "soc",
    "grid_charge": "enableGridCharge",
    "generate": "enableGeneration",
    "sell": "enableSell",
}


def test_tou_field_matches_the_published_model():
    """TOU_FIELD is exactly DEYE's documented per-slot model, name for name."""
    failed = False
    if TOU_FIELD != DEYE_TOU_WIRE_FIELDS:
        missing = {k: v for k, v in DEYE_TOU_WIRE_FIELDS.items() if TOU_FIELD.get(k) != v}
        extra = {k: v for k, v in TOU_FIELD.items() if k not in DEYE_TOU_WIRE_FIELDS}
        print(f"ERROR: TOU_FIELD no longer matches the published model - wrong/missing {missing}, unexpected {extra}")
        failed = True
    assert not failed, "test_tou_field_matches_the_published_model"


def test_deye_const_shape():
    """Constants expose the keys the component relies on."""
    failed = False
    for dc in ("eu", "am", "india"):
        if dc not in DEYE_BASE_URLS or not DEYE_BASE_URLS[dc].startswith("https://"):
            print(f"ERROR: base url missing/invalid for {dc}")
            failed = True
    for ep in ("token", "station_list", "station_device", "device_latest", "config_battery", "tou_update", "dynamic_control", "order_result"):
        if ep not in DEYE_ENDPOINTS:
            print(f"ERROR: endpoint {ep} missing")
            failed = True
    for m in ("selling_first", "zero_export_load", "zero_export_ct"):
        if m not in DEYE_WORKMODE:
            print(f"ERROR: workmode {m} missing")
            failed = True
    for k in ("soc", "battery_power", "grid_power", "pv_power", "load_power"):
        if k not in DEYE_TELEMETRY_KEYS:
            print(f"ERROR: telemetry key {k} missing")
            failed = True
    if TOU_SLOT_COUNT != 6:
        print("ERROR: TOU_SLOT_COUNT must be 6")
        failed = True
    if FREEZE_EXPORT_SOC != 99:
        print("ERROR: FREEZE_EXPORT_SOC must be 99")
        failed = True
    assert not failed, "test_deye_const_shape"


def run_deye_const_tests(my_predbat):
    """Run all DEYE constants tests."""
    failed = False
    for name, fn in [
        ("const_shape", test_deye_const_shape),
        ("tou_field_model", test_tou_field_matches_the_published_model),
    ]:
        try:
            if fn():
                print(f"  FAILED: deye_const.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in deye_const.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
