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
    for f in ("time", "power", "soc", "grid_charge"):
        if f not in TOU_FIELD:
            print(f"ERROR: TOU field {f} missing")
            failed = True
    if TOU_SLOT_COUNT != 6:
        print("ERROR: TOU_SLOT_COUNT must be 6")
        failed = True
    if FREEZE_EXPORT_SOC != 99:
        print("ERROR: FREEZE_EXPORT_SOC must be 99")
        failed = True
    assert not failed, "test_deye_const_shape"
